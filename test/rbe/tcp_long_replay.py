import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import qualification_report as reporting


def main():
    out, arm, source, label, raw, proof = sys.argv[1:]
    out, raw, proof = Path(out), Path(raw), Path(proof)
    captured = reporting.capture(raw, out / "events.jsonl", Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    events = out / "events.jsonl"
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
    proof.mkdir(exist_ok=True, parents=True)
    case = "TestReceiveMemoryAtBufferLimit"
    records = [x for report in captured["reports"] if report["name"] == "test.xml"
               for x in ET.parse(out / report["path"]).iter("testcase") if x.get("name") == case]
    messages = [line.strip() for report in captured["reports"] if report["name"] == "test.log"
                for line in (out / report["path"]).read_text().splitlines()
                if "available after charging segment overhead: got" in line or "accepted another data segment" in line]
    ran = all(not r["cached_locally"] and not r["execution_info"].get("cachedRemotely")
              and r["execution_info"].get("strategy") == "remote" for r in verdict["executed"])
    complete = not verdict["missing"] and not verdict["errors"] and len(records) == 1 and ran
    match arm:
        case "prototype":
            observed = (verdict["status"] == "FAIL" and verdict["case_counts"]["failed"] == 1
                        and records and records[0].find("failure") is not None and len(messages) >= 2)
        case "correction":
            observed = (verdict["status"] == "PASS" and verdict["case_counts"]["passed"] > 1 and not messages
                        and records and records[0].find("skipped") is None)
    verdict.update(arm=arm,diagnostics=messages,matched_expectation=bool(complete and observed))
    reporting.write_json(proof / (arm + ".json"), verdict)
    print(arm,verdict["status"],verdict["matched_expectation"], messages,verdict["errors"])
    return int(not verdict["matched_expectation"])

if __name__ == "__main__":
    sys.exit(main())
