"""Identify both full suite results and the dense-hook carrier in their original XMLs."""
import json
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from xml.etree import ElementTree

from verify import verified_file


def main(temp: Path) -> None:
    out=temp/"phase-results"
    results={}
    req={"internal":{"TestCompletionOrderAndLateWait","TestPooledCallbacksReleaseReferences"},
         "public":{"TestMapAliasing","TestCycles","TestDeadlock","TestLoadHooks","TestRestoreHookDense"}}
    owners={"internal":"//pkg/state:state_test","public":"//pkg/state/tests:tests_test"}
    expected=None
    for fa,source in (("stock","0f8c6a39e32b3185585ac13649b701a182ef7cde"),("compact","67edf06d5c1ccd0746bf96866a15fef2edea5614")):
        events=[json.loads(l) for l in (temp/f"func-{fa}.bep").open()]
        base=out/"functional"/fa
        child,=[e["started"]["uuid"] for e in events if "started" in e]
        code=int((base/"exit").read_text())
        part=results[fa]=dict(child=child,source=source,exit=code,owners={})
        census={}
        for name,label in owners.items():
            row,=[e["testResult"] for e in events if e.get("id",{}).get("testResult",{}).get("label")==label]
            files={f["name"]:verified_file(f,base/(name+"-"+f["name"])) for f in row["testActionOutput"] if f["name"] in ("test.log","test.xml")}
            assert set(files)=={"test.log","test.xml"} and row["executionInfo"]["strategy"]=="remote"
            assert not row.get("cachedLocally") and not row["executionInfo"].get("cachedRemotely")
            xml=ElementTree.parse(base/(name+"-test.xml"))
            cases=[(t.get("name"),"FAIL" if t.find("failure") is not None or t.find("error") is not None else "SKIP" if t.find("skipped") is not None else "PASS") for t in xml.iter("testcase")]
            census[label]=sorted(cases)
            selected={n:status for n,status in cases if n in req[name]}
            part["owners"][label]=dict(files=files,status=row["status"],counts={s:sum(t==s for _,t in cases) for s in ("PASS","SKIP","FAIL")},selected=selected)
            if name=="public":
                lines=(base/(name+"-test.log")).read_text().splitlines()
                vals=[l.strip() for l in lines if re.search(r"^BenchmarkRestoreHookDense(?:-\d+)?\s+32\s+[0-9.]+\s+ns/op",l.strip())]
                part["owners"][label]["benchmark"]=vals
        (out/"functional.json").write_text(json.dumps(results,indent=1)+"\n")
        assert code==0 and all(p["status"]=="PASSED" and p["counts"]["FAIL"]==0 for p in part["owners"].values())
        for name,label in owners.items():
            assert part["owners"][label]["selected"]=={v:"PASS" for v in req[name]}
        assert len(part["owners"][owners["public"]]["benchmark"])==1
        if expected is None: expected=census
        assert census==expected
    results["verified"]=datetime.now(timezone.utc).isoformat()
    (out/"functional.json").write_text(json.dumps(results,indent=1)+"\n")


if __name__=="__main__":
    main(Path(sys.argv[1]))
