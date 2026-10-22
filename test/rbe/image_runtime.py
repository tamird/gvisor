"""Configure a source-built runtime and verify Make image rebuild/reload."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

@contextmanager
def prepared_runtime(destination: Path, runtime_directory: Path, configuration: dict):
    """Keep the temporary runtime registered for exactly the consumer scope."""
    os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
    runtime_name = configuration["runtime_name"]
    image_component = configuration["image_name"]
    records = {"runtime": runtime_name, "status": "INCOMPLETE", "commands": []}
    config_path = Path("/etc/docker/daemon.json")
    original_bytes = config_path.read_bytes()
    original_config = json.loads(original_bytes)
    if runtime_name in original_config.get("runtimes", {}):
        raise RuntimeError("Task runtime name is already occupied")
    records["config_before"] = original_config
    outer_deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])
    deadline = outer_deadline - configuration.get("cleanup_reserve_seconds", 0)
    daemon_pid = None
    daemon_start_ticks = None
    daemon_child = None
    daemon_arguments = None
    daemon_environment = None
    daemon_executable = None
    daemon_cwd = None
    daemon_ready = False
    seccomp_changed = False

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Runtime preparation exhausted the existing work deadline")
        return seconds


    def run(arguments):
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining())
        records["commands"].append({"arguments": arguments, "native_exit": result.returncode})
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode and result.stdout:
            print(result.stdout, end="")
        result.check_returncode()
        return result.stdout


    def record_file(path):
        data = path.read_bytes()
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


    def reload_daemon():
        pid = int(Path("/var/run/docker.pid").read_text())
        process = Path("/proc") / str(pid)
        arguments = [part.decode() for part in (process / "cmdline").read_bytes().split(b"\0") if part]
        if (process / "exe").resolve() != Path(shutil.which("dockerd")).resolve() or Path(arguments[0]).name != "dockerd":
            raise RuntimeError("Docker PID does not identify the worker's dockerd")
        os.kill(pid, signal.SIGHUP)

    def start_ticks(process: Path) -> str:
        return (process / "stat").read_text().rsplit(")", 1)[1].split()[19]

    def stop_daemon() -> None:
        nonlocal daemon_pid, daemon_child
        if daemon_pid is None:
            return
        if daemon_child is not None and daemon_child.poll() is not None:
            records.setdefault("daemon_exits", []).append({"pid": daemon_pid, "native_exit": daemon_child.returncode})
            daemon_pid = None
            daemon_child = None
            return
        process = Path("/proc") / str(daemon_pid)
        if ((daemon_child is None and int(Path("/var/run/docker.pid").read_text()) != daemon_pid) or
                start_ticks(process) != daemon_start_ticks or
                (process / "exe").resolve() != daemon_executable):
            raise RuntimeError("Refuse to stop a daemon with a changed process identity")
        if (daemon_child is None or daemon_ready) and run(["docker", "ps", "--quiet"]).strip():
            raise RuntimeError("Refuse to restart Docker while containers are active")
        os.kill(daemon_pid, signal.SIGTERM)
        stop_deadline = min(deadline, time.monotonic() + 30)
        while process.exists() and time.monotonic() < stop_deadline:
            try:
                state = (process / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.1)
        else:
            if process.exists():
                raise RuntimeError("Owned dockerd did not stop within 30 seconds")
        if daemon_child is not None:
            daemon_child.wait(timeout=min(5, remaining()))
        records.setdefault("daemon_stops", []).append(daemon_pid)
        daemon_pid = None
        daemon_child = None

    def start_daemon(expected_security: list[str], restoring: bool = False) -> dict:
        nonlocal daemon_pid, daemon_start_ticks, daemon_child, daemon_ready
        if daemon_pid is not None:
            raise RuntimeError("Refuse to start a second Docker daemon")
        pidfile = Path("/var/run/docker.pid")
        if pidfile.exists():
            process = Path("/proc") / pidfile.read_text().strip()
            if process.exists() and (process / "stat").read_text().rsplit(")", 1)[1].split()[0] != "Z":
                raise RuntimeError("Another daemon appeared before the owned restart")
        # The restored service outlives this scope. Keep its live output out of
        # the frozen evidence; the fixture daemon is stopped before sealing.
        log = runtime_directory.parent / "dockerd-restored.log" if restoring else destination.parent / "dockerd-fixture.log"
        daemon_ready = False
        with log.open("ab") as output:
            daemon_child = subprocess.Popen(daemon_arguments, executable=str(daemon_executable),
                                            env=daemon_environment, cwd=daemon_cwd, stdin=subprocess.DEVNULL,
                                            stdout=output, stderr=output)
        daemon_pid = daemon_child.pid
        daemon_start_ticks = start_ticks(Path("/proc") / str(daemon_pid))
        records.setdefault("daemon_starts", []).append({"pid": daemon_pid, "start_ticks": daemon_start_ticks})
        ready_deadline = min(deadline, time.monotonic() + 30)
        while time.monotonic() < ready_deadline:
            if daemon_child.poll() is not None:
                raise RuntimeError("Replacement dockerd exited before readiness")
            try:
                result = subprocess.run(["docker", "info", "--format={{json .}}"],
                                        capture_output=True, text=True, timeout=min(5, remaining()))
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0:
                info = json.loads(result.stdout)
                try:
                    observed_pid = int(pidfile.read_text())
                except FileNotFoundError:
                    observed_pid = None
                    records.setdefault("daemon_readiness_observations", []).append(
                        {"pid": daemon_pid, "restoring": restoring, "pidfile": "absent",
                         "docker_info_exit": result.returncode})
                if (observed_pid == daemon_pid and
                        info["SecurityOptions"] == expected_security):
                    for key in ("ID", "DockerRootDir", "Driver", "ServerVersion"):
                        if info[key] != records["server_info_before"][key]:
                            raise RuntimeError("Restart changed Docker identity: " + key)
                    daemon_ready = True
                    return info
            time.sleep(0.1)
        raise RuntimeError("Docker did not expose the exact fixture seccomp profile")

    try:
        records["server_before"] = json.loads(run(["docker", "version", "--format={{json .Server}}"]))
        records["server_info_before"] = json.loads(run(["docker", "info", "--format={{json .}}"]))
        make = ["make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
                "BAZEL_OPTIONS=" + shlex.join(["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
                    "--downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"]]),
                "STARTUP_OPTIONS=--output_base=" + os.environ["APPLICABILITY_OUTPUT_BASE"] + " --bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"],
                "REMOTE_IMAGE_PREFIX=qualification.invalid/images"]
        run([*make, "RUNTIME_DIR=" + str(runtime_directory), "refresh"])
        runsc = runtime_directory / "runsc"
        if not runsc.is_file() or not (runtime_directory / "gvisor-bin").is_dir():
            raise RuntimeError("Canonical Make refresh omitted runsc or sidecars")
        records["runtime_files"] = [record_file(runsc), *[
            record_file(path) for path in sorted((runtime_directory / "gvisor-bin").iterdir()) if path.is_file()]]
        logs = destination.parent / "runsc-logs"
        logs.mkdir()
        run([str(runsc), "install", "--config_file=" + str(config_path),
             "--runtime=" + runtime_name, "--download-sidecars=NEVER", "--require-sidecars=ALWAYS", "--",
             *configuration["runtime_flags"], "--debug-log=" + str(logs) + "/", "--TESTONLY-test-name-env=RUNSC_TEST_NAME"])
        installed = json.loads(config_path.read_text())
        for key, value in original_config.items():
            if key != "runtimes" and installed.get(key) != value:
                raise RuntimeError("Runtime installation changed unrelated daemon configuration")
        for name, value in original_config.get("runtimes", {}).items():
            if installed["runtimes"].get(name) != value:
                raise RuntimeError("Runtime installation changed an existing runtime")
        records["config_after"] = installed
        if profile_name := configuration.get("seccomp_profile"):
            if records["server_before"]["Version"] != configuration["docker_version"]:
                raise RuntimeError("Fixture seccomp profile does not match the Docker version")
            security = records["server_info_before"]["SecurityOptions"]
            if "name=seccomp,profile=builtin" not in security or "seccomp-profile" in original_config:
                raise RuntimeError("Fixture requires the pinned built-in Docker seccomp profile")
            daemon_pid = int(Path("/var/run/docker.pid").read_text())
            process = Path("/proc") / str(daemon_pid)
            daemon_arguments = [part.decode() for part in (process / "cmdline").read_bytes().split(b"\0") if part]
            daemon_executable = (process / "exe").resolve()
            daemon_cwd = (process / "cwd").resolve(strict=True)
            daemon_start_ticks = start_ticks(process)
            if (daemon_executable != Path(shutil.which("dockerd")).resolve() or
                    Path(daemon_arguments[0]).name != "dockerd" or
                    any(arg.startswith("--seccomp-profile") for arg in daemon_arguments)):
                raise RuntimeError("Worker daemon identity or seccomp options differ")
            # Keep the existing private environment in memory; never retain it.
            daemon_environment = dict(item.decode().split("=", 1) for item in
                                      (process / "environ").read_bytes().split(b"\0") if item)
            records["daemon_arguments"] = daemon_arguments
            records["daemon_executable"] = str(daemon_executable)
            records["daemon_cwd"] = str(daemon_cwd)
            records["seccomp_profile"] = record_file(Path(profile_name))
            installed["seccomp-profile"] = profile_name
            config_path.write_text(json.dumps(installed, indent=2) + "\n")
            seccomp_changed = True
            # Docker 24 reads its default seccomp profile only at startup.
            # Preserve its argv, environment, data directory and configuration.
            stop_daemon()
            expected_security = ["name=seccomp,profile=" + profile_name if value ==
                                 "name=seccomp,profile=builtin" else value for value in security]
            records["fixture_server_info"] = start_daemon(expected_security)
            records["daemon_action"] = "Restart sole disposable daemon with the pinned fixture profile"
        else:
            reload_daemon()
            records["daemon_action"] = "Reload existing daemon runtimes"
        ready_deadline = min(time.monotonic() + remaining(), time.monotonic() + 30)
        while time.monotonic() < ready_deadline:
            result = subprocess.run(["docker", "info", "--format={{json .}}"], capture_output=True, text=True, timeout=min(5, remaining()))
            if result.returncode == 0:
                info = json.loads(result.stdout)
                if runtime_name in info["Runtimes"]:
                    records["server_info"] = info
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError("Docker did not expose the configured runtime")
        if configuration.get("lifecycle_only", False):
            records["status"] = "PASS"
            yield records
            return
        image_name = "gvisor.dev/images/" + image_component + ":latest"
        image_key = image_component.replace("/", "_")
        image_make = make
        records["image_platform"] = "--config=x86_64"
        run([*image_make, "rebuild-" + image_key])
        built = json.loads(run(["docker", "image", "inspect", image_name]))[0]
        records["built_image"] = built
        contents = json.loads(Path(configuration["contents"]).read_text())
        inputs = next(row["runtime_inputs"] for row in contents["architectures"] if row["architecture"] == "amd64")
        expected_source = run(["git", "rev-parse", "HEAD"]).strip()
        if inputs["status"] != "PASS" or inputs["source"] != expected_source:
            raise RuntimeError("Runtime image inputs lack exact-source verification")
        retained_archive = record_file(Path(inputs["archive"]["path"]))
        if retained_archive != inputs["archive"] or built["Id"] != "sha256:" + inputs["config_sha256"]:
            raise RuntimeError("Make image differs from the inspected archive")
        records["runtime_inputs"] = inputs
        local_tags = [tag for tag in built["RepoTags"] if tag.startswith("gvisor.dev/images/" + image_component + ":")]
        if built["Architecture"] != "amd64" or image_name not in local_tags or len(local_tags) != 2:
            raise RuntimeError("Make produced unexpected architecture or local tags")
        run(["docker", "image", "rm", *local_tags])
        run([*image_make, "SKIP_IMAGE_LOAD=true", "load-" + image_key])
        loaded = json.loads(run(["docker", "image", "inspect", image_name]))[0]
        if loaded["Id"] != built["Id"] or not set(local_tags).issubset(loaded["RepoTags"]):
            raise RuntimeError("Cached Make load did not restore the identical image")
        records["image"] = {"id": built["Id"], "architecture": built["Architecture"],
                            "tags": local_tags, "rebuild": "PASS", "cached_restore": "PASS"}
        records["status"] = "PASS"
        yield records
    finally:
        deadline = outer_deadline
        try:
            # This disposable worker has one owner. Restore its original
            # runtime configuration even if installation or the consumer fails.
            config_path.write_bytes(original_bytes)
            if seccomp_changed:
                stop_daemon()
                records["restored_server_info"] = start_daemon(records["server_info_before"]["SecurityOptions"], restoring=True)
                if records["restored_server_info"]["Runtimes"] != records["server_info_before"]["Runtimes"]:
                    raise RuntimeError("Cleanup did not restore the original Docker runtimes")
            else:
                reload_daemon()
            if config_path.read_bytes() != original_bytes:
                raise RuntimeError("Cleanup did not restore the original Docker config bytes")
            records["cleanup"] = "PASS"
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            records["cleanup"] = "FAIL"
            records["cleanup_error"] = str(error)
            raise
        finally:
            destination.write_text(json.dumps(records, indent=2) + "\n")
