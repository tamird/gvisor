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
if verdict["errors"] or verdict["missing"] or set(labels) != set(verdict["expected_labels"]):
    why.append("incomplete original evidence or wrong target")
if len(results) != len(labels) or any(item.get("cachedLocally") or item.get("executionInfo", {}).get("cachedRemotely") or item.get("executionInfo", {}).get("strategy") != "remote" for item in results):
    why.append("expected one uncached remote original per target")
runner_exit = int((out/"runner_exit").read_text())
cases={v.split("/")[-1] for failure in verdict["failures"] for v in failure.get("cases",())}
expected={"TestSignedZeroSlices","short0","short1"}
observations=[line.strip()[:350] for line in logs.splitlines() if "Graphs are not isomorphic:" in line]
if verdict["status"]!="FAIL" or runner_exit==0 or cases!=expected or len(observations)!=2:
    why.append("expected two failed signed-zero comparisons on stock and no unrelated failures")
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
