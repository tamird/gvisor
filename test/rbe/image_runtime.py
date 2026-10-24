"""Configure a source-built runtime and verify Make image rebuild/reload."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import tarfile
import urllib.request
import uuid

import qualification_report as report
from image_archive import archive


def image_make_arguments() -> list[str]:
    """Use the same native image and Bazel configuration as runtime setup."""
    return ["make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
            "BAZEL_OPTIONS=--config=x86_64 --compilation_mode=opt --lockfile_mode=error --noannounce_rc --downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"],
            "STARTUP_OPTIONS=--output_base=" + os.environ["APPLICABILITY_OUTPUT_BASE"] + " --bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"],
            "REMOTE_IMAGE_PREFIX=qualification.invalid/images"]


def prepare_benchmark_helpers(destination: Path, directory: Path, utility: dict) -> dict:
    """Build declared Hey and load the immutable temporary readiness fixture."""
    os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
    records = {"status": "INCOMPLETE", "commands": [], "hey": {}, "util": {}}

    def run(arguments: list[str]) -> str:
        seconds = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Benchmark image preparation exhausted the work deadline")
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=seconds)
        records["commands"].append({"arguments": arguments, "native_exit": result.returncode,
                                    "stdout": result.stdout, "stderr": result.stderr})
        result.check_returncode()
        return result.stdout

    def inspect(name: str) -> dict:
        match json.loads(run(["docker", "image", "inspect", name])):
            case [{"Os": "linux", "Architecture": "amd64"} as image]:
                return image
            case other:
                raise ValueError(f"Expected one Linux AMD64 helper image: {name}: {other}")

    try:
        config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
        records["source"] = run(["git", "rev-parse", "HEAD"]).strip()
        if records["source"] != config["source"]:
            raise ValueError("Benchmark helpers differ from the qualified source")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "hey-image.tar"
        make = image_make_arguments()
        run([*make, "copy", "TARGETS=//images/benchmarks:hey_archive", "DESTINATION=" + str(path)])
        hey = records["hey"]
        hey["target"] = "//images/benchmarks:hey_archive"
        image_config, _, _ = archive(path, hey)
        if image_config["os"] != "linux" or image_config["architecture"] != "amd64":
            raise ValueError("Source-built Hey archive has the wrong platform")
        hey["name"] = "gvisor.dev/images/benchmarks/hey:latest"
        run([*make, "rebuild-benchmarks_hey"])
        hey["image"] = inspect(hey["name"])
        if hey["image"]["Id"] != "sha256:" + hey["config_sha256"]:
            raise ValueError("Make loaded a different Hey image than its source archive")

        util = records["util"]
        util["supplier"] = utility
        # This temporary baseline fixture is selected by manifest and config.
        # A failed pull must not fall back to its mutable apt-based Dockerfile.
        reference = utility["immutable_image"]
        if (not reference.endswith("@" + utility["manifest_digest"]) or
                not utility["manifest_digest"].startswith("sha256:") or
                not utility["image_config_digest"].startswith("sha256:")):
            raise ValueError("Readiness helper lacks an immutable manifest/config identity")
        run(["docker", "pull", "--platform=linux/amd64", reference])
        pulled = inspect(reference)
        if pulled["Id"] != utility["image_config_digest"]:
            raise ValueError("Pulled readiness helper differs from its pinned config")
        util["name"] = "gvisor.dev/images/benchmarks/util:latest"
        run(["docker", "tag", reference, util["name"]])
        util["image"] = inspect(util["name"])
        if util["image"]["Id"] != pulled["Id"]:
            raise ValueError("Tagged readiness helper differs from its pinned image")
        container = "qualification-wget-version-" + uuid.uuid4().hex
        try:
            util["version"] = run(["docker", "run", "--name=" + container, "--runtime=runc",
                                   pulled["Id"], "wget", "--version"])
            if not util["version"].startswith("GNU Wget "):
                raise ValueError("Readiness helper did not report its actual GNU Wget version")
        finally:
            cleanup = subprocess.run(["docker", "rm", "--force", container], capture_output=True, text=True, timeout=30)
            util["cleanup"] = {"native_exit": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
            if cleanup.returncode and "No such container" not in cleanup.stderr:
                raise RuntimeError("Failed to remove the owned Wget version container")
        records["status"] = "PASS"
        return records
    finally:
        destination.write_text(json.dumps(records, indent=2) + "\n")

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
    daemon_changed = False

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

    def start_daemon(expected_security: list[str], expected_experimental: bool, restoring: bool = False) -> dict:
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
                        info["SecurityOptions"] == expected_security and
                        info["ExperimentalBuild"] == expected_experimental):
                    for key in ("ID", "DockerRootDir", "Driver", "ServerVersion"):
                        if info[key] != records["server_info_before"][key]:
                            raise RuntimeError("Restart changed Docker identity: " + key)
                    daemon_ready = True
                    return info
            time.sleep(0.1)
        raise RuntimeError("Docker did not expose the exact fixture startup configuration")

    try:
        records["server_before"] = json.loads(run(["docker", "version", "--format={{json .Server}}"]))
        records["server_info_before"] = json.loads(run(["docker", "info", "--format={{json .}}"]))
        make = image_make_arguments()
        retained = configuration.get("retained_release_files")
        if retained is None:
            run([*make, "RUNTIME_DIR=" + str(runtime_directory), "refresh"])
        else:
            # The caller joins retained files to a successful configured
            # //:release before entering this scope. Image checks stay below.
            for name, expected in retained.items():
                path = runtime_directory / name
                path.resolve(strict=True).relative_to(runtime_directory.resolve())
                actual = record_file(path)
                if any(actual[key] != expected[key] for key in ("bytes", "sha256")):
                    raise ValueError("Retained release file changed: " + name)
            records["release_supplier"] = configuration["release_supplier"]
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
        profile_name = configuration.get("seccomp_profile")
        experimental = configuration.get("docker_experimental", False)
        experimental_changed = experimental and not records["server_info_before"]["ExperimentalBuild"]
        if (profile_name or experimental) and records["server_before"]["Version"] != configuration["docker_version"]:
            raise RuntimeError("Fixture startup settings do not match the Docker version")
        if profile_name or experimental_changed:
            security = records["server_info_before"]["SecurityOptions"]
            if profile_name and ("name=seccomp,profile=builtin" not in security or "seccomp-profile" in original_config):
                raise RuntimeError("Fixture requires the pinned built-in Docker seccomp profile")
            daemon_pid = int(Path("/var/run/docker.pid").read_text())
            process = Path("/proc") / str(daemon_pid)
            daemon_arguments = [part.decode() for part in (process / "cmdline").read_bytes().split(b"\0") if part]
            daemon_executable = (process / "exe").resolve()
            daemon_cwd = (process / "cwd").resolve(strict=True)
            daemon_start_ticks = start_ticks(process)
            changed_flags = ({"--seccomp-profile"} if profile_name else set()) | ({"--experimental"} if experimental_changed else set())
            if (daemon_executable != Path(shutil.which("dockerd")).resolve() or
                    Path(daemon_arguments[0]).name != "dockerd" or
                    any(arg.split("=", 1)[0] in changed_flags for arg in daemon_arguments)):
                raise RuntimeError("Worker daemon identity or startup options differ")
            # Keep the existing private environment in memory; never retain it.
            daemon_environment = dict(item.decode().split("=", 1) for item in
                                      (process / "environ").read_bytes().split(b"\0") if item)
            records["daemon_arguments"] = daemon_arguments
            records["daemon_executable"] = str(daemon_executable)
            records["daemon_cwd"] = str(daemon_cwd)
            if profile_name:
                records["seccomp_profile"] = record_file(Path(profile_name))
                installed["seccomp-profile"] = profile_name
            if experimental_changed:
                installed["experimental"] = True
            config_path.write_text(json.dumps(installed, indent=2) + "\n")
            daemon_changed = True
            # Docker 24 installs experimental routes and its seccomp profile at startup.
            # Preserve its argv, environment, data directory and configuration.
            stop_daemon()
            expected_security = ["name=seccomp,profile=" + profile_name if profile_name and value ==
                                 "name=seccomp,profile=builtin" else value for value in security]
            records["fixture_server_info"] = start_daemon(
                expected_security, experimental or records["server_info_before"]["ExperimentalBuild"])
            records["daemon_action"] = "Restart sole disposable daemon with declared fixture startup settings"
        else:
            reload_daemon()
            records["daemon_action"] = "Reload existing daemon runtimes"
        ready_deadline = min(time.monotonic() + remaining(), time.monotonic() + 30)
        while time.monotonic() < ready_deadline:
            result = subprocess.run(["docker", "info", "--format={{json .}}"], capture_output=True, text=True, timeout=min(5, remaining()))
            if result.returncode == 0:
                info = json.loads(result.stdout)
                if runtime_name in info["Runtimes"] and (not experimental or info["ExperimentalBuild"]):
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
            if daemon_changed:
                stop_daemon()
                records["restored_server_info"] = start_daemon(
                    records["server_info_before"]["SecurityOptions"],
                    records["server_info_before"]["ExperimentalBuild"], restoring=True)
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


def materialize_retained_release(supplier: dict, directory: Path, evidence: Path, prefix: str) -> dict:
    """Verify the retained release owner before fetching its seven runtime files."""
    records = {"status": "INCOMPLETE", "supplier": supplier, "files": {}}

    def fetch(item: dict, destination: Path) -> None:
        remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Retained runtime retrieval exhausted the work deadline")
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": item["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(180, remaining)) as response:
            data = response.read(item["bytes"] + 1)
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Retained runtime supplier identity mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    try:
        archive_path = directory.parent / "runtime-supplier.tar.gz"
        fetch(supplier["bundle"], archive_path)
        with tarfile.open(archive_path) as archive:
            names = [entry.name for entry in archive if entry.isfile()]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate retained runtime evidence member")
            phase = "invocation-paired-harness/"
            verdict = json.load(archive.extractfile(phase + "verdict.json"))
            if verdict["source"] != supplier["source"] or verdict["status"] != "PASS" or verdict["native_exit"] != 0:
                raise ValueError("Retained runtime build lacks exact-source native success")
            owners = [row for row in verdict["expected"] if row["label"] == "//:release"]
            if owners != [supplier["owner"]]:
                raise ValueError("Retained runtime configured release owner differs")
            events = evidence / (prefix + "-runtime-supplier-events.jsonl")
            events.write_bytes(archive.extractfile(phase + "events.jsonl").read())
            report.write_json(evidence / (prefix + "-runtime-supplier-verdict.json"), verdict)
        owner = report.Owner(supplier["owner"]["label"], supplier["owner"]["configuration"])
        outputs = report.advertised_build_outputs(events, {(owner, "default")})
        by_name = {row["name"].split("/release/", 1)[1]: row for row in outputs}
        expected = {"runsc", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server",
                    "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor-sentry-prewarmer",
                    "gvisor-bin/gvisor-proc-umounter", "gvisor-bin/gvisor_sentry"}
        if len(outputs) != 8 or set(by_name) != expected | {"containerd-shim-runsc-v1"} or set(supplier["files"]) != expected:
            raise ValueError("Retained release output set differs")
        records["release_outputs"] = outputs
        for name, item in supplier["files"].items():
            if any(by_name[name][key] != item[key] for key in ("bytes", "sha256")):
                raise ValueError("Retained runtime file differs from its default-BEP output: " + name)
            # The BEP advertises materialized files. CAS availability is checked
            # here, using their authenticated content digests, without fallback.
            artifact = {**item, "uri": f"bytestream://remote.buildbuddy.io/blobs/{item['sha256']}/{item['bytes']}"}
            path = directory / name
            fetch(artifact, path)
            path.chmod(0o755)
            records["files"][name] = {**report.file_record(path), "uri": artifact["uri"]}
        records["status"] = "PASS"
        return {name: {key: row[key] for key in ("bytes", "sha256")} for name, row in records["files"].items()}
    finally:
        report.write_json(evidence / (prefix + "-runtime-supplier.json"), records)
