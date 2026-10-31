import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import qualification_report as report

work, arm, source, fixture, label, public = sys.argv[1:]
work, public = Path(work), Path(public)
case_names = {
    "linux": {"TestTimeWaitSynAck"},
    "proposed": {"TestForwarderTimeWaitSynAck", "TestForwarderReopenAfterTimeWait"},
    "fixed": {"TestForwarderTimeWaitSynAck", "TestForwarderReopenAfterTimeWait"},
}[arm]
out = public / arm
out.mkdir(parents=True, exist_ok=True)
measure = {"source": source, "fixture": fixture, "label": label, "arm": arm, "expected_cases": sorted(case_names)}
errors = []
try:
    capture = report.capture(work/"raw.bep", work/"events.jsonl", Path("bazel-out").resolve(), work/"reports")
    (work/"capture_exit").write_text(str(int(bool(capture["errors"])))+"\n")
    ctx = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    ctx.update(expected_source=source, command_kind="test", execution_finished_at=(work/"finished").read_text().strip())
    report.write_json(work/"context.json", ctx)
    (work/"targets").write_text(label+"\n")
    verdict = report.verdict(work/"events.jsonl", report.manifest_path(work/"events.jsonl"), work/"context.json", work/"targets", ())
    report.write_json(out/"bazel.json",verdict)
    events = [json.loads(line) for line in (work/"events.jsonl").read_text().splitlines()]
    results = [e["testResult"] for e in events if "testResult" in e and e["id"]["testResult"]["label"] == label]
    measure["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
    measure["strategy"] = [r.get("executionInfo",{}).get("strategy") for r in results]
    if verdict["status"] != "PASS":
        errors.append(f"Bazel {verdict['status']}: {verdict['errors']}")
    if len(results) != 1 or measure["strategy"] not in (["local"],["standalone"]) or any(r.get("cachedLocally") or r.get("executionInfo",{}).get("cachedRemotely") for r in results):
        errors.append("expected a single uncached test on the preflighted Linux machine")
    for item in capture["reports"]:
        if item["name"] in ("test.xml","test.log"):
            (out/item["name"]).write_bytes((work/item["path"]).read_bytes())
    logs = (out/"test.log").read_text(errors="replace")
    passed = set(re.findall(r"--- PASS: ([^\s]+) \(",logs))
    measure["passed_log_cases"] = sorted(passed)
    skipped_or_failed = re.findall(r"--- (?:SKIP|FAIL): ([^\s]+)",logs)
    if not case_names <= passed or skipped_or_failed:
        errors.append(f"missing {sorted(case_names-passed)}, skipped/failed {skipped_or_failed}")
    metrics = [(kind,int(flags)) for kind,flags in re.findall(r"TIMEWAIT-PROBE kind=(reopen|synack|old-fin) flags=(\d+)",logs)]
    measure["observations"] = metrics
    # Preserve the measurements even if a peer behaves differently from the proposed conclusion.
    expected = {"linux":{"synack":16,"old-fin":16},"proposed":{"synack":4,"old-fin":4,"reopen":18},"fixed":{"synack":16,"old-fin":16,"reopen":18}}[arm]
    if len(metrics) != len(expected) or dict(metrics) != expected:
        errors.append(f"expected {expected}, actual {metrics}")
    measure["packet_lines"] = [l.strip() for l in logs.splitlines() if "TIMEWAIT-PROBE kind=" in l]
except (OSError, ValueError, KeyError, AssertionError, ET.ParseError) as e:
    errors.append(f"{type(e).__name__}: {e}")
finally:
    log = work/"bazel.log"
    if log.exists():
        lines = log.read_text(errors="replace").splitlines()
        measure["diagnostics"] = ["\n".join(lines[i:i+5])[:1100] for i,line in enumerate(lines) if line.startswith("ERROR:")][:12]
    measure["bazel_version"] = (work/"version").read_text().strip() if (work/"version").is_file() else None
    measure["errors"] = errors
    measure["status"] = "FAIL" if errors else "PASS"
    report.write_json(out/"result.json",measure)
    print(json.dumps(measure),flush=True)
sys.exit(bool(errors))
