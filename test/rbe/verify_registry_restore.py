import json
import xml.etree.ElementTree as ET
from pathlib import Path
import sys

import qualification_report as reporting


out, arm, source, target_text, raw, proof = sys.argv[1:]
out, raw, proof = Path(out), Path(raw), Path(proof)
labels = target_text.split()
(out / "targets.txt").write_text("\n".join(labels) + "\n")
events = out / "events.jsonl"
captured = reporting.capture(raw, events, Path("bazel-out"), out / "reports")
(out / "capture_exit").write_text("1" if captured["errors"] else "0")
identity = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
identity.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
reporting.write_json(out / "context.json", identity)
verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
records = [json.loads(line) for line in events.read_text().splitlines()]
results = [r["testResult"] for r in records if "testResult" in r]
proof.mkdir(exist_ok=True, parents=True)
why = []
if verdict["errors"] or verdict["missing"] or set(labels) != set(verdict["expected_labels"]):
    why.append("incomplete original evidence or wrong target")
if len(results) != len(labels) or any(item.get("cachedLocally") or item.get("executionInfo", {}).get("cachedRemotely") or item.get("executionInfo", {}).get("strategy") != "remote" for item in results):
    why.append("expected one uncached remote original per target")
runner_exit = int((out/"runner_exit").read_text())
wanted = {f"//test/syscalls:{name}_test_runsc_systrap_directfs_save": case for name, case in
          (("shm", "MprotectWriteOnWritableSegmentSucceeds"), ("msgqueue", "MsgCtlIpcSet"), ("semaphore", "SemIpcSet"))}
restores = {}
case_names = {}
for item in captured["reports"]:
    label = item["identity"]["label"]
    path = out / item["path"]
    if item["name"] == "test.log":
        # The runner logs each command just before it executes it and fails if
        # restore exits nonzero. Native PASS below confirms they all completed.
        restores[label] = sum("Executing: [" in line and " restore " in line and " --image-path " in line
                              for line in path.read_text().splitlines())
    if item["name"] == "test.xml":
        case_names[label] = [node.get("name", "").rsplit(".", 1)[-1] for node in ET.parse(path).iter("testcase")
                             if node.find("skipped") is None and node.get("status") != "notrun" and node.get("result") != "suppressed"]
if set(labels) != set(wanted) or verdict["status"] != "PASS" or runner_exit or verdict["case_counts"].get("skipped",0):
    why.append("restore command, selection or skip")
for label, name in wanted.items():
    if case_names.get(label) != [name] or restores.get(label,0) < 2:
        why.append("missing exact case and multiple completed restore commands: " + label)
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed={"restore_commands":restores, "executed_cases":case_names}, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm+".json"), verdict)
print(json.dumps({k:verdict[k] for k in ("arm","invocation","status","case_counts","verified","verification_errors","execution_to_verdict_seconds")}))
sys.exit(bool(why))
