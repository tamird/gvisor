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
import re
observations=[l[l.index("TLP_WIRE "):].strip() for l in logs.splitlines() if "TLP_WIRE " in l]
expected={"DuplicateACK":False,"MatchingDSACK":False,"NoDuplicate":True,"UnrelatedDSACK":True,"WindowUpdate":True}
seen={}
for line in observations:
    m=re.fullmatch(r"TLP_WIRE (\w+) before=(\d+) after=(\d+) ca=(\d+) unacked=(\d+)",line)
    if m:
        name=m[1]; before,after,ca,unacked=map(int,m.groups()[1:])
        seen[name]=ca
        if name not in expected or (after<before)!=expected[name] or (arm=="fixed-native" and unacked==0):why.append(f"{name}: thresholds or flight disagree with trace")
if set(seen)!=set(expected) or len(observations)!=5:why.append("did not execute every expected wire trace")
failed=[name for name,loss in expected.items() if loss and seen.get(name)!=0]
if arm=="stock-netstack":
    if sorted(failed)!=sorted(n for n in expected if expected[n]) or any(seen.get(n)!=3 for n in failed) or verdict["status"]!="FAIL" or not runner_exit or logs.count("congestion state after probe repaired the loss")!=3:
        why.append("published PR did not reproduce three stale recovery states")
elif failed or verdict["status"]!="PASS" or runner_exit or verdict["case_counts"].get("passed",0)==0:why.append("wire regression did not pass")
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
