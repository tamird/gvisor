"""Compile only the original parser translation unit. No link or LLVM build."""
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

prior = "41d0faaf-0ed2-474b-93d7-6f9745ae03f4"
new = "da0ec3e0-44ec-47bf-8ee7-79e086bb94bf"
label = "//tools/nvidia_driver_differ:driver_ast_parser"
temp = Path(os.environ["RUNNER_TEMP"])
dest = temp / "compiler-owners"
dest.mkdir(exist_ok=True)
report = {"source": os.environ["PRODUCT"], "target": label, "prior": prior, "candidate": new,
          "outputGroup": "compilation_outputs", "status": "INCOMPLETE"}

def save():
    (dest / "probe.json").write_text(json.dumps(report, indent=2) + "\n")

def remote(iid, scoped=True):
    lookup = {"invocationId": iid}
    if scoped:
        lookup["targetLabel"] = label
    req = urllib.request.Request("https://app.buildbuddy.io/rpc/BuildBuddyService/GetExecution",
        data=json.dumps({"executionLookup": lookup}).encode(), headers={"Content-Type": "application/json",
        "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp).get("execution", [])

def interested(rr):
    result = []
    for r in rr:
        if r.get("actionMnemonic") != "CppCompile" or r.get("targetLabel") != label:
            continue
        em = r.get("executedActionMetadata", {})
        result.append({"digest": r.get("actionDigest"), "output": r.get("primaryOutputPath"),
            "rpcCode": r.get("status", {}).get("code", 0), "exit": r.get("exitCode", 0),
            "assignedRAM": r.get("taskSize", {}).get("estimatedMemoryBytes"),
            "peakRAM": em.get("usageStats", {}).get("peakMemoryBytes"),
            "start": em.get("executionStartTimestamp"), "end": em.get("executionCompletedTimestamp")})
    return result

def safe_lines(log):
    terms = ("Resource Exhausted", "signal: killed", "No space left on device", "Build completed",
             "Build did NOT complete", "Elapsed time:", "processes:", "No registered executors",
             "no registered executors", "no matching executors", "command was killed",
             "DeadlineExceeded", "failed to parse", "no such target", "Unrecognized option")
    return [s[-450:] for s in log.read_text(errors="replace").splitlines()
            if any(t in s for t in terms) and
            not any(k in s.lower() for k in ("key", "header", "token", "authorization", "bearer"))][-15:]

save()
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == report["source"]
before = report["priorParserExecutions"] = interested(remote(prior))
save()
failed = [r for r in before if r["rpcCode"] == 8]
if not failed:
    raise SystemExit("No resource-limited parser C++ action in original")
original_ram = max(int(r["assignedRAM"] or 0) for r in failed)
# Escalate scheduling allocation on the existing executor only. Stop if the
# original allocation leaves no meaningful next step within this fixed limit.
budget = min(max(8_000_000_000, 2 * original_ram), 12_000_000_000)
if original_ram >= budget:
    raise SystemExit("Already assigned at least the bounded maximum")
report["requestedRAM"] = budget
save()
log = temp / "parser-unit.log"
bazel = ["bazelisk", "--bazelrc=" + str(temp / "images.bazelrc"), "build", "--config=x86_64",
         "--invocation_id=" + new, "--output_groups=compilation_outputs", "--remote_retries=0",
         "--remote_exec_header=x-buildbuddy-platform.estimatedmemory=" + str(budget),
         "--color=no", "--curses=no", label]
with log.open("w") as f:
    result = subprocess.run(["timeout", "--signal=INT", "--kill-after=20s", "20m", *bazel],
                            stdout=f, stderr=subprocess.STDOUT)
report["bazelExit"] = result.returncode
report["diagnostics"] = safe_lines(log)
save()
after = report["candidateParserExecutions"] = interested(remote(new))
if not after:
    all_actions = remote(new, scoped=False)
    report["remoteActionCount"] = len(all_actions)
    report["blockingDependencies"] = [
        {"mnemonic": r.get("actionMnemonic"), "target": r.get("targetLabel"),
         "output": r.get("primaryOutputPath"), "code": r.get("status", {}).get("code"),
         "ram": r.get("taskSize", {}).get("estimatedMemoryBytes")}
        for r in all_actions if r.get("status", {}).get("code")][:8]
save()
old = {r["digest"]["hash"] for r in failed}
report["matchedSuccessfulAction"] = any(r["digest"]["hash"] in old and r["rpcCode"] == 0 and
                                        r["exit"] == 0 for r in after)
if not result.returncode:
    objects = {}
    for obj in Path("bazel-bin/tools/nvidia_driver_differ/_objs/driver_ast_parser").glob("driver_ast_parser*.o"):
        body = obj.read_bytes()
        assert body[:6] == b"\x7fELF\x02\x01" and int.from_bytes(body[16:18], "little") == 1
        assert int.from_bytes(body[18:20], "little") == 62
        objects[obj.name] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
    report["objects"] = objects
    assert objects
report["status"] = "PASS" if not result.returncode and report["matchedSuccessfulAction"] else "NO PAIRED PASS"
save()
raise SystemExit(report["status"] != "PASS")
