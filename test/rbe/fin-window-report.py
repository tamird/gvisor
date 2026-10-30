"""Reconcile the original Bazel results and the packet testbench's named cases."""
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import qualification_report as reporting

def required(suite):
    if suite == "tcp_outside_the_window":
        cases = {"TestWindowWithUnreadFIN/FINOnly", "TestWindowWithUnreadFIN/DataAndFIN"}
        for func, flags in (("TestTCPOutsideTheWindow", ("SYN", "SYNACK", "ACK", "FIN", "Data")), ("TestAckOTWSeqInClosing", ("SYN", "SYNACK", "ACK", "FINACK", "Data"))):
            cases.update(f"{func}/{flag}{offset}" for flag in flags for offset in range(3))
        return cases
    return {f"{func}/{name}:offset={offset}" for func in ("TestEstablishedUnaccSeqAck","TestPassiveCloseUnaccSeqAck","TestActiveCloseUnaccpSeqAck","TestSimultaneousCloseUnaccSeqAck") for name in ("OTWSeq", "UnaccAck") for offset in range(3)}

def main():
    phase, root, source, code, *targets = sys.argv[1:]
    root = Path(root); work = root / phase
    out = {"source":source,"phase":phase,"exit":int(code),"checks":{},"errors":[]}
    try:
        raw=work/"raw.jsonl"
        cap=reporting.capture(raw,work/"events.jsonl",Path("bazel-out").resolve(),work/"reports")
        out["errors"] += cap["errors"]
        records=[json.loads(line) for line in (work/"events.jsonl").read_text().splitlines()]
        out["invocation"]=next((e["started"]["uuid"] for e in records if "started" in e),None)
        for t in targets:
            base=t.rsplit(":",1)[1]
            testlog=Path("bazel-testlogs")/t.removeprefix("//").replace(":","/")
            result=[e["testResult"] for e in records if e.get("id",{}).get("testResult",{}).get("label")==t and "testResult" in e]
            s=[e["testSummary"]["overallStatus"] for e in records if e.get("id",{}).get("testSummary",{}).get("label")==t and "testSummary" in e]
            logs=(testlog/"test.log").read_text(errors="replace") if (testlog/"test.log").is_file() else ""
            detail={"result":[r["status"] for r in result],"summary":s,"diagnostics":[line[:450] for line in logs.splitlines() if re.search(r"--- FAIL|^FAIL| [^ ]+_test.go:[0-9]+:|[Oo]peration not permitted|bootstrap failed",line)][:35]}
            assert len(result)==1 and result[0]["status"]=="PASSED" and s==["PASSED"],(t,detail)
            r=result[0]; assert not r.get("cachedLocally") and not r.get("executionInfo",{}).get("cachedRemotely"),(t,"cached")
            if phase == "wire":
                assert r.get("executionInfo",{}).get("strategy") in {"local","standalone"},(t,r.get("executionInfo"))
                suite=base.removesuffix("_native_test").removesuffix("_netstack_test")
                expect=required(suite)
                passed=set(re.findall(r"--- PASS: ([^\s]+) \(",logs))
                detail["passed"]=sorted(passed)
                detail["missing"]=sorted(expect-passed)
                assert not detail["missing"],(t,detail["missing"])
                assert not re.search(r"--- (?:FAIL|SKIP):",logs),t
            else:
                xml=ET.parse(testlog/"test.xml")
                cases=xml.findall(".//testcase")
                detail["cases"]=len(cases)
                detail["skips"]=[c.get("name") for c in cases if c.find("skipped") is not None]
                assert cases and not any(c.find("error") is not None or c.find("failure") is not None for c in cases),t
            out["checks"][t]=detail
    except Exception as e:
        out["errors"].append(str(e)[:4000])
    out["verified"]=not out["errors"] and int(code)==0 and len(out["checks"])==len(targets)
    (root/(phase+".json")).write_text(json.dumps(out,indent=2)+"\n")
    return int(not out["verified"])

if __name__ == "__main__":
    sys.exit(main())
