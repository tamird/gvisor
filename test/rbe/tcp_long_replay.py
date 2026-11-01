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
    cases = [x for report in captured["reports"] if report["name"] == "test.xml" for x in ET.parse(out / report["path"]).iter("testcase")]
    rows = {x.get("name").split("/")[-1]: "FAIL" if x.find("failure") is not None else "SKIP" if x.find("skipped") is not None else "PASS"
            for x in cases if x.get("name","").split("/")[-1] in ("cookies=true","cookies=false")}
    logs = [line.strip() for report in captured["reports"] if report["name"] == "test.log"
            for line in (out / report["path"]).read_text().splitlines() if any(t in line for t in ("drained handshake payload","tcp_test.go:","FAIL: TestReceiveCharge", "PASS: TestReceiveCharge"))]
    messages = [line for line in logs if "drained handshake payload still causes queue drops" in line]
    ran = all(not r["cached_locally"] and not r["execution_info"].get("cachedRemotely") and r["execution_info"].get("strategy") == "remote" for r in verdict["executed"])
    expect = "FAIL" if arm=="prototype" else "PASS"
    complete = not verdict["missing"] and not verdict["errors"] and ran and rows == {n: expect for n in ("cookies=true","cookies=false")}
    observed = verdict["status"]==expect and (len(messages)>=2 if arm=="prototype" else not messages)
    verdict.update(arm=arm,diagnostics=logs[:35],handshakes=rows,matched_expectation=bool(complete and observed))
    reporting.write_json(proof / (arm + ".json"), verdict)
    print(arm,verdict["status"],verdict["matched_expectation"], logs,verdict["errors"])
    return int(not verdict["matched_expectation"])

if __name__ == "__main__":
    sys.exit(main())
