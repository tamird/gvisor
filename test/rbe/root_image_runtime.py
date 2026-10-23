"""Prepare the existing containerd harness and check its cleanup boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
evidence = Path(sys.argv[1])
mode = sys.argv[2]
records = {"status": "INCOMPLETE", "commands": []}
destination = evidence / ("root-runtime-" + mode + ".json")
config_path = Path("/etc/docker/daemon.json")


def run(arguments: list[str]) -> str:
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Root consumer preparation exhausted the work deadline")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining)
    records["commands"].append({"arguments": arguments, "native_exit": result.returncode})
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode and result.stdout:
        print(result.stdout, end="")
    result.check_returncode()
    return result.stdout


try:
    if mode == "prepare":
        records["server"] = json.loads(run(["docker", "version", "--format={{json .Server}}"]))
        records["daemon_config"] = config_path.read_text()
        records["containers_before"] = sorted(run(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
        make = ["make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
                "BAZEL_OPTIONS=--config=x86_64 --compilation_mode=opt --lockfile_mode=error --noannounce_rc --downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"],
                "STARTUP_OPTIONS=--output_base=" + os.environ["APPLICABILITY_OUTPUT_BASE"] + " --bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"],
                "REMOTE_IMAGE_PREFIX=qualification.invalid/images"]
        records["images"] = []
        for name in ("busybox", "symlink-resolv"):
            component = "basic/" + name
            image_name = "gvisor.dev/images/" + component + ":latest"
            image_key = component.replace("/", "_")
            staged = Path("/tmp/gvisor-cri-images") / (image_key + ".tar")
            if staged.exists():
                raise RuntimeError("A pre-existing CRI image export could mask the selected image: " + str(staged))
            contents = json.loads((evidence / (name + "-contents.json")).read_text())
            row = next(row for row in contents["architectures"] if row["architecture"] == "amd64")
            if contents["status"] != "PASS" or row["runtime_inputs"]["source"] != run(["git", "rev-parse", "HEAD"]).strip():
                raise RuntimeError("Runtime image lacks exact-source closure")
            run([*make, "rebuild-" + image_key])
            built = json.loads(run(["docker", "image", "inspect", image_name]))[0]
            if built["Id"] != "sha256:" + row["config_sha256"]:
                raise RuntimeError("Make image differs from inspected archive: " + name)
            tags = [tag for tag in built["RepoTags"] if tag.startswith("gvisor.dev/images/" + component + ":")]
            if built["Architecture"] != "amd64" or image_name not in tags or len(tags) != 2:
                raise RuntimeError("Make produced unexpected local tags or architecture")
            run(["docker", "image", "rm", *tags])
            run([*make, "SKIP_IMAGE_LOAD=true", "load-" + image_key])
            loaded = json.loads(run(["docker", "image", "inspect", image_name]))[0]
            if loaded["Id"] != built["Id"] or not set(tags).issubset(loaded["RepoTags"]):
                raise RuntimeError("Cached Make load failed to restore the identical image")
            records["images"].append({"component": component, "id": built["Id"], "tags": tags,
                                      "runtime_inputs": row["runtime_inputs"], "rebuild": "PASS", "cached_restore": "PASS"})
        # This is the existing test fixture, with one supported version selected.
        # The product images above are built from source through Bazel.
        run([*make, "DOCKER_BUILD_ARGS=--build-arg CONTAINERD_MATRIX=2.2.3:v1.34.0", "rebuild-containerd_harness"])
        harness = json.loads(run(["docker", "image", "inspect", "gvisor.dev/images/containerd/harness:latest"]))[0]
        records["harness"] = harness
        if harness["Architecture"] != "amd64" or harness["Config"]["Entrypoint"] != ["/usr/local/bin/entrypoint.sh"]:
            raise RuntimeError("Existing containerd harness platform or entrypoint differs")
        if config_path.read_text() != records["daemon_config"]:
            raise RuntimeError("Image preparation changed host daemon configuration")
    elif mode == "check":
        prepared = json.loads((evidence / "root-runtime-prepare.json").read_text())
        records["containers_after"] = sorted(run(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
        records["daemon_config_unchanged"] = config_path.read_text() == prepared["daemon_config"]
        if records["containers_after"] != prepared["containers_before"] or not records["daemon_config_unchanged"]:
            raise RuntimeError("Root tests left containers or changed the outer daemon configuration")
    else:
        raise ValueError("Unexpected root runtime phase: " + mode)
    records["status"] = "PASS"
finally:
    destination.write_text(json.dumps(records, indent=2) + "\n")
