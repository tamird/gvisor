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
# Count the original 31 handshake subcases and the independent SACK case.
import xml.etree.ElementTree as ET
xmls = [out / rec["path"] for rec in captured["reports"] if rec["name"] == "test.xml"]
names = [(case.get("classname", ""), case.get("name","")) for path in xmls for case in ET.parse(path).iter("testcase")]
# report by full xUnit name; testing.Go can place its suite in classname.
import collections, re
observations = sorted("/".join(filter(None, item)) for item in names)
group_counts = collections.Counter()
for full in observations:
    match = re.search(r"TestTCPSynCookie/flags=([^/]+)", full)
    if match:
        group_counts[match[1]] += 1
if (len(set(observations)) != 32 or len(observations) != 32
        or sum(full.endswith("TestSynCookieSACK") for full in observations) != 1
        or sum(full.endswith("TestTCPSynCookie") for full in observations) != 1
        or sorted(group_counts.values()) != [5]*6
        or verdict["case_counts"] != {"passed":32,"skipped":0,"failed":0}):
    why.append("missing or unexpected cookie-handshake testcase or disposition")
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
