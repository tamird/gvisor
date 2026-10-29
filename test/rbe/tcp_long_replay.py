import json
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
logs = "\n".join((out / r["path"]).read_text() for r in captured["reports"] if r["name"] == "test.log")
why = []
if verdict["errors"] or verdict["missing"] or verdict["skips"] or set(labels) != set(verdict["expected_labels"]):
    why.append("incomplete original evidence, wrong target, or unexpected skip")
if len(results) != len(labels) or any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") or r.get("executionInfo", {}).get("strategy") != "remote" for r in results):
    why.append("expected one uncached remote original per target")
runner_exit = int((out / "runner_exit").read_text())
note = "no zero-window probe at the original deadline"
observations = [s.strip() for s in logs.splitlines() if note in s or ("--- PASS" in s and "TestZeroWindowSend" in s)]
if arm == "stock":
    if (verdict["status"] != "FAIL" or verdict["native_exit"] != 3 or not runner_exit or
        verdict["case_counts"].get("failed", 0) != 1 or verdict["case_counts"].get("passed", 0) != 0 or
        note not in logs or "--- FAIL: TestZeroWindowSend" not in logs or
        any(not case.endswith("TestZeroWindowSend") for row in verdict["failures"] for case in row.get("cases", []))):
        why.append("stock did not fail precisely at the old probe deadline")
elif verdict["status"] != "PASS" or runner_exit != 0 or any(r["status"] != "PASSED" for r in results):
    why.append("candidate target failed")
elif arm == "correction" and (verdict["case_counts"].get("passed") != 1 or "--- PASS: TestZeroWindowSend" not in logs):
    why.append("the original probe and reopened byte stream were not checked")
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
