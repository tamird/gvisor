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

    def remaining():
        seconds = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
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

    try:
        records["server_before"] = json.loads(run(["docker", "version", "--format={{json .Server}}"]))
        make = ["make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
                "OPTIONS=--compilation_mode=opt --lockfile_mode=error --noannounce_rc --downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"],
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
        try:
            # This disposable worker has one owner. Restore its original
            # runtime configuration even if installation or the consumer fails.
            config_path.write_bytes(original_bytes)
            reload_daemon()
            records["cleanup"] = "PASS"
        except (OSError, RuntimeError):
            records["cleanup"] = "FAIL"
            raise
        finally:
            destination.write_text(json.dumps(records, indent=2) + "\n")
