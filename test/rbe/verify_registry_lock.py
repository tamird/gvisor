import json
import re
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
architectures = ("amd64", "arm64")
sections = {}
case_names = {}
for item in captured["reports"]:
    label = item["identity"]["label"]
    path = out / item["path"]
    if label.endswith("_nogo") and item["name"] == "test.log":
        parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", path.read_text())
        sections[label] = {arch: body.strip().splitlines() for arch, body in zip(parts[1::2], parts[2::2])}
        if len(parts) != 5 or set(sections[label]) != set(architectures):
            why.append("missing original architecture results for "+label)
    if not label.endswith("_nogo") and item["name"] == "test.xml":
        case_names[label] = [
            node.get("name","") for node in ET.parse(path).iter("testcase")
            if node.find("skipped") is None and node.get("status")!="notrun" and node.get("result")!="suppressed"]
if set(sections) != {label for label in labels if label.endswith("_nogo")}:
    why.append("missing analyzer report")
for label, logs in sections.items():
    for arch, lines in logs.items():
        if arm == "negative":
            pattern = r"checklocks: pkg/sentry/kernel/msgqueue/msgqueue\.go:\d+:\d+: must hold .+\.Mu exclusively .* to call ObjectCount, but not held .*GOOARCH=" + arch + r", GOOS=linux\)"
            if len(lines) != 1 or not re.fullmatch(pattern,lines[0]):
                why.append("expected the unlocked ObjectCount call: "+arch)
        elif lines != ["PASS"]:
            why.append("unexpected analyzer output: "+label+" "+arch)
if arm == "negative":
    if verdict["status"] != "FAIL" or verdict["native_exit"] != 3 or runner_exit != 1 or not verdict["failures"]:
        why.append("the deliberately unguarded call did not fail its analyzer")
elif verdict["status"] != "PASS" or runner_exit:
    why.append("expected passing original results")
if arm == "fixed":
    samples = {":semaphore_test":"TestBasic", ":semaphore_test_runsc_systrap_directfs":"SemGet",
               ":msgqueue_test_runsc_systrap_directfs":"MsgGet", ":shm_test_runsc_systrap_directfs":"LookupByKey"}
    for suffix, sample in samples.items():
        if not any(label.endswith(suffix) and any(name.rsplit(".",1)[-1]==sample for name in names)
                   for label, names in case_names.items()):
            why.append("missing executed test "+sample)
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed={"sections":sections, "executed_cases":case_names}, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm+".json"), verdict)
print(json.dumps({k:verdict[k] for k in ("arm","invocation","status","case_counts","verified","verification_errors","execution_to_verdict_seconds")}))
sys.exit(bool(why))
