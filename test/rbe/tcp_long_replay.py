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
if verdict["status"] != "PASS" or runner_exit != 0 or verdict["case_counts"].get("passed", 0) == 0:
    why.append("package did not pass or no cases were executed")
import re
observations = [line[line.index("TLP_EPISODE "):].strip() for line in logs.splitlines() if "TLP_EPISODE " in line]
if arm!="suites":
    parsed={}
    for line in observations:
        m=re.fullmatch(r"TLP_EPISODE (pending|earlier|later|hole) offset=(\d+) elapsed_ms=(\d+) timeouts=(\d+) state=(\d+) sack=(\d+) tlp=(\d+)",line)
        if m: parsed[m[1]]=tuple(map(int,m.groups()[1:]))
    if len(observations)!=4 or len(parsed)!=4: why.append("missing one or more packet observations")
    for k,(off,ms,rto,state,sack,tlp) in parsed.items():
        expect_rto = (arm=="master" and k=="earlier") or (arm!="master" and k=="pending") or (arm=="published" and k in ("earlier","later"))
        if off!=(20 if k=="pending" else 30): why.append(f"{k}: wrong retransmitted offset {off}")
        if k=="hole":
            if arm=="fixed" and state==0: why.append("outstanding SACK lost the disorder state")
            continue
        if bool(rto)!=expect_rto or (ms>=1000)!=expect_rto:
            why.append(f"{k}: expected timeout {expect_rto}, got {rto} after {ms}ms")
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
