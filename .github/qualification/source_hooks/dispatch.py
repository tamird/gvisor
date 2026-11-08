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
        note({"offline_failure_annotation": [c for c in ("source-presubmit", "source-presubmit-arm64") if "`" + c + "`" in msg]})
        sys.exit(0)
    if case.startswith("legacy-") and args == ["artifact", "upload", str(w / "legacy-log" / "test.log")]:
        note({"offline_legacy_log_upload": True})
        sys.exit(0)
    note({"unexpected_agent_call": True})
    sys.exit(78)
assert cmd in ("bazel", "bazelisk")
launcher = str(w / "bazelisk")
if args == ["shutdown"]:
    note({"shutdown": True})
    if case == "shutdown-fails":
        sys.exit(27)
    os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    os.execv(launcher, [launcher, "--nosystem_rc", "--nohome_rc", "shutdown"])
config = next(s.split("=", 1)[1] for s in args if s.startswith("--config="))
assert config in ("source-presubmit", "source-presubmit-arm64") and args[0] == "test"
fail = (case == "first-fails" and config == "source-presubmit") or (case == "second-fails" and config.endswith("arm64"))
note({"test": config, "launcher": cmd, "stamp_policy": "USE_BAZEL_VERSION" not in os.environ
      and os.environ.get("USE_BAZEL_FALLBACK_VERSION") == "error:",
      "reports_errors": "--test_output=errors" in args})
flags = (w / "local-flags").read_text().splitlines()
os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin"
# The production commands select their original profile. The offline fixture
# limits both to the same architecture-independent sentinel and local strategy.
bep = w / (case + "-" + config + ".bep")
os.execv(launcher, [launcher, "--nosystem_rc", "--nohome_rc", *args, *flags,
                    "--target_pattern_file=" + str(w / "sentinel.targets"),
                    "--build_event_json_file=" + str(bep), "--nocache_test_results", "--flaky_test_attempts=1",
                    "--test_env=HOOK_SENTINEL=" + ("fail" if fail else "pass")])
