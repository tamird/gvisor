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
    logs="\n".join((out / r["path"]).read_text() for r in captured["reports"] if r["name"]=="test.log")
    rows=re.findall(r"--- (PASS|FAIL): TestListenerReusesReceiveBuffer/connection(\d+)",logs)
    expected="FAIL" if arm=="prototype" else "PASS"
    ran=all(not r["cached_locally"] and not r["execution_info"].get("cachedRemotely") and r["execution_info"].get("strategy")=="remote" for r in verdict["executed"])
    observed=(bool(rows) and rows[0]==("PASS","0") and rows[-1][0]=="FAIL" and "Poll returned 0 ready file descriptors" in logs) if arm=="prototype" else (rows==[("PASS",str(i)) for i in range(8)])
    complete=not verdict["missing"] and not verdict["errors"] and ran
    diagnostics=[l.strip()[:300] for l in logs.splitlines() if any(t in l for t in ("Poll returned","waiting for SYN-ACK:","--- FAIL:", "iptables", "operation not permitted","no such file","failed to create"))]
    verdict.update(arm=arm,handshakes=rows,diagnostics=diagnostics[:14],matched_expectation=bool(complete and observed and verdict["status"]==expected))
    reporting.write_json(proof / (arm + ".json"), verdict)
    print(arm,verdict["status"],verdict["matched_expectation"], diagnostics[:14],verdict["errors"])
    return int(not verdict["matched_expectation"])

if __name__ == "__main__":
    sys.exit(main())
