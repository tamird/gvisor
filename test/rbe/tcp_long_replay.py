import hashlib
import json
import re
from xml.etree import ElementTree
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
expected = {"Large": 32768, "Fits": 1312, "Straddles": 1313, "MSSMinusOne": 1419, "MSS": 1420,
            "MSSPlusOne": 1421, "FullTail": 32768, "ChangedMark": 32768, "Fin": 0}
observations = {}
outputs = {}
case_names = []
for item in captured["reports"]:
    if item["name"] not in ("test.log", "test.xml"):
        continue
    data = (out / item["path"]).read_bytes()
    outputs[item["name"]] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if item["name"] == "test.xml":
        (proof / (arm + ".xml")).write_bytes(data)
        case_names = [c.get("name") for c in ElementTree.fromstring(data).iter("testcase")]
for name, second in expected.items():
    boundary = re.findall(rf"QUEUED_BOUNDARY name={name} first=(\d+) second=(\d+) prefix=(\d+) prefix_packets=(\d+) window=(\d+) first_payload=(\d+)", logs)
    fin = re.findall(rf"QUEUED_FIN name={name} bytes=(\d+) next_packets=(\d+)", logs)
    case = "TestQueuedWriteBoundary/" + name
    if len(boundary) != 1 or len(fin) != 1 or case_names.count(case) != 1:
        why.append("missing or ambiguous original boundary/FIN/named case: " + case)
        continue
    first, nxt, prefix, packets, window, payload = map(int, boundary[0])
    total, next_packets = map(int, fin[0])
    expected_first = 24 * 1420 if name == "FullTail" else 32768
    observations[name] = {"first": first, "second": nxt, "prefix": prefix, "prefix_packets": packets,
                          "window": window, "first_payload": payload, "fin_after": total, "next_packets": next_packets}
    if ((first, nxt, prefix, packets, window, total) != (expected_first, second, 32660, 23, 1420, expected_first + second)
            or not 0 < payload <= min(1420, expected_first - 32660 + second)):
        why.append("inconsistent measured bytes: " + name)
    # Linux's handling of a mark change is observed independently. The mark
    # control binds the gVisor experiment to the stock per-write policy.
    if name == "ChangedMark" and arm == "native":
        continue
    full = name in ("Fits", "FullTail") or (name != "Fin" and arm == "native") or (
        name in ("Large", "MSSPlusOne") and arm == "candidate")
    if payload != (1420 if full else 108):
        why.append("unexpected first wire payload: " + name)
if set(outputs) != {"test.log", "test.xml"} or verdict["case_counts"]["skipped"] or verdict["case_counts"]["failed"]:
    why.append("an original report or successful case is missing")
verdict["original_outputs"] = outputs
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
