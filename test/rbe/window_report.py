from datetime import datetime, timezone
from pathlib import Path
import json, os, re, sys
from qualification_report import xml_cases

d=Path(sys.argv[1])
source=os.environ["CANDIDATE"]
expected=("c5e5c744ad8b01597886bbfe7bcfe999e3cde6ca49de7c580a5fa53b6acce9f5","65609")
result={"source":source,"stock_wire_original":expected,"arms":{},"failures":[]}
for arm,owners in (("candidate-wire",("unit",)),("candidate-race",("unit","suite"))):
    p=d/arm
    try:
        def get(name): return (p/name).read_text().strip()
        inv=re.findall(r"Invocation ID: ([0-9a-f-]{36})",get("bazel-diagnostics"))
        r=dict(exit=int(get("exit")),end=get("end"),invocation=inv,owners={})
        result["arms"][arm]=r
        if r["exit"] or get("source") != source or get("source-after") != source or get("source-before-status") or get("source-after-status"):
            raise ValueError("source or Bazel exit")
        for owner in owners:
            cases=xml_cases(p/f"{owner}.test.xml")
            r["owners"][owner]={k:cases[k] for k in ("passed","failed","skipped")}
            if not cases["passed"] or cases["failed"]: raise ValueError(f"{arm}/{owner} missing/failed")
        if arm=="candidate-wire":
            fingerprints=re.findall(r"STREAM-WIRE sha=([0-9a-f]{64}) bytes=(\d+)",get("unit.test.log"))
            r["wire"]=fingerprints
            if set(fingerprints)!={expected}: raise ValueError("filtered public checkpoint bytes differ")
    except (KeyError,OSError,ValueError) as exc: result["failures"].append(arm+": "+str(exc))
result["verified"]=datetime.now(timezone.utc).isoformat()
(d/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
if result["failures"]: raise SystemExit(result["failures"])
