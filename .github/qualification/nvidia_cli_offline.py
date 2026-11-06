#!/usr/bin/python3
"""Run a Bazel CLI from its own working directory with neither host Git nor a default route."""
import errno
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys


if sys.argv[1] != "--isolated":
    # Preserve only the environment needed by the Bazel launcher and the
    # selected driver rule. The program runs as the invoking user.
    preserve = ("BUILD_WORKSPACE_DIRECTORY", "BUILD_WORKING_DIRECTORY", "RUNFILES_DIR",
                "RUNFILES_MANIFEST_FILE", "RUNFILES_MANIFEST_ONLY", "TEST_SRCDIR",
                "JAVA_RUNFILES", "NVIDIA_DRIVER_SOURCES")
    params = [n + "=" + os.environ[n] for n in preserve if n in os.environ]
    command = ["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--net", "/usr/bin/setpriv",
               "--reuid=" + str(os.getuid()), "--regid=" + str(os.getgid()), "--clear-groups",
               "/usr/bin/env", "-i", "PATH=/path-without-git", "LANG=C.UTF-8", *params,
               "/usr/bin/python3", str(Path(__file__).resolve()), "--isolated", *sys.argv[1:]]
    os.execv(command[0], command)

binary, *args = sys.argv[2:]
assert os.geteuid() != 0 and shutil.which("git") is None
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
    peer.setblocking(False)
    no_route = peer.connect_ex(("192.0.2.1", 1)) == errno.ENETUNREACH
assert no_route
versions = [args[args.index(flag) + 1] for flag in ("--base", "--next")]
assert versions in (["580.95.05", "615.71.09"], ["615.71.09", "620.70.06"])
index = json.loads(os.environ.get("NVIDIA_DRIVER_SOURCES", "{}"))
evidence = {"route": "ENETUNREACH", "git_on_path": False, "index_size": len(index), "headers": {}}
for v in versions:
    anchor = Path(index.get(v) or "/missing-nvidia-anchor")
    hdr = anchor.parent / "src/common/sdk/nvidia/inc/nvos.h"
    row = {"copying": anchor.is_file(), "header": hdr.is_file()}
    if hdr.is_file():
        text = hdr.read_text(errors="replace")
        end = text.find("} NV_CHANNEL_GROUP_ALLOCATION_PARAMETERS;")
        if end >= 0:
            s = text[text.rfind("typedef struct", 0, end):end]
            row["contains_reserved2"] = bool(re.search(r"\bNvHandle\s+reserved2\s*;", s))
            row["contains_internalFlags"] = bool(re.search(r"\bNvU32\s+internalFlags\s*;", s))
    evidence["headers"][v] = row
print("NVIDIA_OFFLINE " + json.dumps(evidence), file=sys.stderr, flush=True)
os.execv(binary, [binary, *args])
