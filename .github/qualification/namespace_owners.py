import json
import os
from pathlib import Path
import re
import shutil
import subprocess

draft = "fdd009fd9c0752b3f9fc3bf1a8c3deeff105a8c3"
fixed = "428e3d67c87363bc16be64c80e8fe58a6a46c467"
out = Path(os.environ["RUNNER_TEMP"]) / "namespace-owners"
cases = {
    "network_namespace_test": "NetworkNamespaceTest.Setns",
    "socket_netlink_route_test": "NetlinkRouteTest.ChangeNetnsAndOtherAttrsTogether",
}
package = Path("test/syscalls/linux")
test_file = package / "network_namespace.cc"
result = {"cases": cases, "results": {}}


def save():
    (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *map(str, args)], text=True).strip()


def fixture():
    build = package / "BUILD"
    with build.open("r+") as f:
        original = f.read()
        f.seek(0)
        f.write('load("@rules_shell//shell:sh_test.bzl", "sh_test")\n' + original)
        for owner, case in cases.items():
            args = [f"$(rootpath :{owner})", case]
            data = [":" + owner, "//:release"]
            f.write(f'\nsh_test(name = "{owner}_guest", srcs = ["namespace_owner.sh"], args = {args!r}, data = {data!r})\n')
    shutil.copy2(out / "namespace_owner.sh", package / "namespace_owner.sh")


def interesting(line):
    return re.search(r"\bERROR:|command not found|No such file|not permitted|[Pp]ermission denied|undefined|failed to|Error:|Failure|Assertion|panic:|registerChild|native syscall launcher|FAILED TO BUILD", line)


def run(phase, labels):
    record = {"targets": {}, "diagnostics": []}
    result["results"][phase] = record
    save()
    events = out / (phase + ".bep")
    log = out / (phase + ".log")
    cmd = [os.environ["BAZEL"], "test", "--config=x86_64", "-c", "opt", "--keep_going",
           "--jobs=3", "--test_sharding_strategy=disabled", "--test_timeout=300",
           "--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
           "--noshow_progress", "--color=no", "--curses=no",
           "--test_env=GUEST_TMP=" + str(out), "--build_event_json_file=" + str(events)]
    with log.open("w") as stream:
        record["bazel_exit"] = subprocess.run(cmd + list(labels), stdout=stream, stderr=subprocess.STDOUT).returncode
    lines = log.read_text(errors="replace").splitlines()
    first = next((i for i, line in enumerate(lines) if "ERROR:" in line), None)
    if first is not None:
        record["first_error_context"] = [line[:800] for line in lines[first:first + 12]]
    record["diagnostics"] = [line[:800] for line in lines if interesting(line)][:24]
    for label, case in labels.items():
        target = {"case": case, "test_attempts": [], "markers": []}
        record["targets"][label] = target
    if events.exists():
        for line in events.open():
            event = json.loads(line)
            key = event.get("id", {})
            if "actionCompleted" in key and not event.get("action", {}).get("success", True):
                record.setdefault("failed_actions", [])
                if len(record["failed_actions"]) < 6:
                    action = event["action"]
                    record["failed_actions"].append({"owner": key["actionCompleted"].get("label"), "type": action.get("type"), "exit": action.get("exitCode"), "message": action.get("failureDetail", {}).get("message", "")[:800]})
            label = key.get("testResult", {}).get("label")
            if label not in labels:
                continue
            target = record["targets"][label]
            if "aborted" in event:
                target["aborted"] = event["aborted"]
            test = event.get("testResult")
            if test:
                target["test_attempts"].append({"status": test.get("status"), "cached": test.get("cachedLocally", False)})
    for label, case in labels.items():
        target = record["targets"][label]
        testlog = Path("bazel-testlogs") / label[2:].replace(":", "/") / "test.log"
        if target["test_attempts"] and testlog.is_file():
            output = testlog.read_text(errors="replace").splitlines()
            target["markers"] = [line[-600:] for line in output if case in line and re.search(r'\[\s*(RUN|OK|FAILED|SKIPPED)\s*\]', line)][:12]
            target["first_lines"] = [line[-600:] for line in output[:32]]
            target["last_lines"] = [line[-600:] for line in output[-16:]]
            target["guest_caps_verified"] = "Verified guest CAP_NET_ADMIN and CAP_SYS_ADMIN" in output
            target["test_errors"] = [line[-800:] for line in output if interesting(line)][:18]
    print(phase, json.dumps(record), flush=True)
    save()
    return record["targets"]


def verdict(target):
    markers = target.get("markers", [])
    ran = any(re.search(r'\[\s*RUN\s*\]', line) for line in markers)
    if not ran or not target.get("guest_caps_verified") or not target.get("test_attempts"):
        return "not_observed"
    if any(re.search(r'\[\s*SKIPPED\s*\]', line) for line in markers):
        return "skipped"
    if any(re.search(r'\[\s*OK\s*\]', line) for line in markers) and target["test_attempts"][-1]["status"] == "PASSED":
        return "passed"
    if target["test_attempts"][-1]["status"] == "FAILED" and any("registerChild called on a non-root namespace" in line for line in target.get("test_errors", [])):
        return "draft_namespace_failure"
    return "unexpected_result"


save()
for system, source in (("draft", draft), ("corrected", fixed)):
    git("checkout", "--detach", source)
    git("restore", "--source=" + fixed, "--", test_file)
    assert git("hash-object", test_file) == git("rev-parse", f"{fixed}:{test_file}")
    result.setdefault("source", {})[system] = {"commit": git("rev-parse", "HEAD"), "test_blob": git("hash-object", test_file)}
    fixture()
    targets = run(system, {f"//test/syscalls/linux:{owner}_guest": case for owner, case in cases.items()})
    result.setdefault("observed", {})[system] = {target["case"]: verdict(target) for target in targets.values()}
    save()
    git("restore", "--", package / "BUILD", test_file)
    (package / "namespace_owner.sh").unlink()
wanted = {"draft": "draft_namespace_failure", "corrected": "passed"}
result["complete"] = all(actual == wanted[system] for system, values in result["observed"].items() for actual in values.values())
save()
raise SystemExit(0 if result["complete"] else 1)
