import json
import re
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
observations = [line[line.index("TLP_FUTURE "):].strip() for line in logs.splitlines() if "TLP_FUTURE " in line]
expected = {"DuplicateACK":False, "MatchingDSACK":False, "NoDuplicate":True, "UnsentData":True,
            "UnrelatedDSACK":True, "WindowUpdate":True}
pattern = (r"TLP_FUTURE (\w+) end=(\d+) mss=(\d+) before=(\d+)/(\d+) atend=(\d+)/(\d+) "
           r"checked=(\d+)/(\d+) after=(\d+)/(\d+) ca=(\d+) unacked=(\d+)")
seen = {}
for line in observations:
    m = re.fullmatch(pattern, line)
    if not m or m[1] in seen or m[1] not in expected:
        why.append("malformed or duplicate measurement")
        continue
    name=m[1]
    end,mss,c0,t0,c1,t1,c2,t2,c3,t3,state,unacked=map(int,m.groups()[1:])
    seen[name] = {"end":end,"invalid_ack":(end+1)%(2**32) if name=="UnsentData" else None,
                  "mss":mss,"cwnd":[c0,c1,c2,c3],"ssthresh":[t0,t1,t2,t3],"ca":state,"unacked":unacked}
    if mss<=0 or min(c0,c1,c2,c3)<=0 or t1!=t0 or state!=0 or (arm=="fixed-native" and unacked==0):
        why.append(f"{name}: initial probe, recovery state, or flight does not match")
    if name=="UnsentData" and arm=="stock-netstack":
        if not (t2<t1 and t3==t2):
            why.append("published sender did not reduce its window on the ACK for unsent data")
    elif t2!=t1 or (t3<t2)!=expected[name]:
        why.append(f"{name}: threshold differs at invalid ACK or real data ACK")
if set(seen)!=set(expected) or len(observations)!=len(expected):
    why.append("did not observe the six tail-probe episodes")
if arm=="stock-netstack":
    if verdict["status"]!="FAIL" or runner_exit==0 or logs.count("ACK for unsent data changed ssthresh")!=1 or logs.count("want reduction=true")!=1:
        why.append("published control did not fail exactly at the two expected assertions")
elif verdict["status"]!="PASS" or runner_exit!=0 or verdict["case_counts"].get("passed",0)==0:
    why.append("Linux/composed wire regression did not pass")
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=seen, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
