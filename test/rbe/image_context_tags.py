"""Compare raw-checkout Make's tags against the files in Bazel's archives."""
import json
import os
from pathlib import Path
import subprocess
import sys


arch, expected, destination = sys.argv[1:]
os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
os.environ["LC_ALL"] = "C"
tags = json.loads(expected)
records = {}
try:
    for image, tag in tags.items():
        # Printing a tag does not execute the image. Skip binfmt registration
        # on the verification host; use Make's real copy, staging and hash.
        command = ["make", "--no-print-directory", "MAKE=make --assume-old=register-cross", "ARCH=" + arch,
                   "DOCKER_BUILD=false", "STARTUP_OPTIONS=--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"],
                   "OPTIONS=--compilation_mode=opt --lockfile_mode=error --noannounce_rc",
                   "local-image-" + image.replace("/", "_")]
        stdout = subprocess.check_output(command, text=True)
        found = [line for line in stdout.splitlines() if line.startswith("gvisor.dev/images/")]
        wanted = "gvisor.dev/images/" + image + ":" + tag
        records[image] = {"expected": wanted, "actual": found}
        if found != [wanted]:
            raise ValueError(f"Raw Make returned {found}, expected {wanted}")
finally:
    Path(destination).write_text(json.dumps({"architecture": arch, "expected": tags, "executed": records}, indent=2) + "\n")
