#!/usr/bin/python3
"""Offline Buildkite service calls and one local Bazel sentinel."""
import json
import os
from pathlib import Path
import sys
import time


w = Path(os.environ["HOOK_TASK"])
case = os.environ["HOOK_CASE"]
cmd = Path(sys.argv[0]).name
args = sys.argv[1:]


def note(data):
    with (w / (case + ".jsonl")).open("a") as stream:
        stream.write(json.dumps(data) + "\n")


if cmd in ("sudo", "docker", "make", "apt", "apt-get", "apt-cache", "sysctl", "systemctl", "gcloud", "killall"):
    subtype = "apt update" if args == ["-E", "apt-get", "update", "-q"] else "ps" if cmd == "docker" and args[:1] == ["ps"] else ""
    note({"forbidden": cmd, "operation": subtype})
    if case.startswith("legacy-"):
        if cmd == "make":
            note({"legacy_make": " ".join(args) if args in (["-s", "testlogs"], ["clean"], ["bazel-shutdown"]) else "other"})
            if args == ["-s", "testlogs"]:
                print("//qualification:legacy", w / "legacy-log" / "test.log")
        return_code = 0 if cmd in ("sudo", "apt-cache", "docker", "make") else 77
        sys.exit(return_code)
    if subtype == "apt update":
        time.sleep(0.25)
    sys.exit(77)
if cmd == "buildkite-agent":
    if args[:2] == ["env", "dump"]:
        os.execv(str(w / "agent"), [str(w / "agent"), *args])
    if args == ["artifact", "download", "tools/bazeldefs/*", "."]:
        note({"offline_download": True})
        sys.exit(0)
    if args == ["annotate", "--style", "error", "--context", "failures", "--append"]:
        msg = sys.stdin.read()
        note({"source_annotation": "mixed test and ARM64 build results" in msg})
        sys.exit(0)
    if case.startswith("legacy-") and args == ["artifact", "upload", str(w / "legacy-log" / "test.log")]:
        note({"offline_legacy_log_upload": True})
        sys.exit(0)
    note({"unexpected_agent_call": True})
    sys.exit(78)
assert cmd == "bazelisk" and case.startswith("status-")
if args == ["shutdown"]:
    note({"shutdown": True})
    sys.exit(0)
assert args[0] in ("test", "build")
rc = int(os.environ["HOOK_MIXED_EXIT" if args[0] == "test" else "HOOK_ARM64_EXIT"])
note({"argv": args, "exit": rc, "pin": os.environ.get("USE_BAZEL_VERSION"),
      "fallback": os.environ.get("USE_BAZEL_FALLBACK_VERSION")})
sys.exit(rc)
