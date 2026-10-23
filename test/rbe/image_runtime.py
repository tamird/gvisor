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
import uuid

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
