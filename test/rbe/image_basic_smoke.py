import json
import os
from pathlib import Path
import subprocess
import sys

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
destination = Path(sys.argv[1])
records = []

def run(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode and result.stdout:
        print(result.stdout, end="")
    result.check_returncode()
    return result.stdout

os.environ["DOCKER_BUILD"] = "false"
os.environ["STARTUP_OPTIONS"] = "--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"]
os.environ["BAZEL_OPTIONS"] = "--lockfile_mode=error --noannounce_rc"
make = ["make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
        "OPTIONS=--lockfile_mode=error --noannounce_rc",
        "STARTUP_OPTIONS=--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"],
        "REMOTE_IMAGE_PREFIX=qualification.invalid/images", "LOCAL_IMAGE_PREFIX=gvisor-qualification/images"]
try:
    run(["docker", "info"])
    for image in ("alpine", "busybox", "tmpfile"):
        name = "basic_" + image
        local = "gvisor-qualification/images/basic/" + image + ":latest"
        run([*make, "rebuild-" + name])
        built = json.loads(run(["docker", "image", "inspect", local]))[0]
        if built["Architecture"] != "amd64":
            raise RuntimeError(f"Incorrect image architecture: {built['Architecture']}")
        local_tags = [tag for tag in built["RepoTags"] if tag.startswith("gvisor-qualification/images/basic/" + image + ":")]
        if local not in local_tags or len(local_tags) != 2:
            raise RuntimeError(f"Expected local hash/latest tags: {local_tags}")
        run(["docker", "image", "rm", *local_tags])
        run([*make, "SKIP_IMAGE_LOAD=true", "load-" + name])
        loaded = json.loads(run(["docker", "image", "inspect", local]))[0]
        if loaded["Id"] != built["Id"] or not set(local_tags).issubset(loaded["RepoTags"]):
            raise RuntimeError("Cached load changed image identity")
        check = "test -f /etc/alpine-release" if image == "alpine" else "busybox true"
        if image == "tmpfile":
            check = 'test "$(cat /tmp/foo/file.txt)" = 123; test "$(stat -c %a /tmp)" = 1777'
        run(["docker", "run", "--rm", "--network=none", local, "sh", "-ec", check])
        records.append({"image": "basic/" + image, "image_id": built["Id"],
                        "architecture": built["Architecture"], "tags": built["RepoTags"],
                        "raw_make_rebuild": "PASS", "raw_make_cached_load": "PASS", "container_check": "PASS"})
finally:
    destination.write_text(json.dumps({"expected": ["basic/alpine", "basic/busybox", "basic/tmpfile"],
                                      "executed": records}, indent=2) + "\n")
