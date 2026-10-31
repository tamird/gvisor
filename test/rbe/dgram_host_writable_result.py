"""Verify the shared datagram case from original outputs, including the timeout."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from sys import argv
from xml.etree import ElementTree as ET

root = Path(argv[1]); problems = []; result = {}
name = "GoferDatagramEpollTest.WakesAfterHostDrain"

def read(path):
    return path.read_text(errors="replace").strip() if path.is_file() else ""

def cases(path):
    try:
        return [{"class":t.get("classname"),"name":t.get("name"),"skipped":t.find("skipped") is not None,
                 "failed":t.find("failure") is not None or t.find("error") is not None}
                for t in ET.parse(path).iter("testcase") if t.get("status") != "notrun"]
    except (ET.ParseError,OSError): return []

for arm in ("native","stock","fixed","owners"):
    d=root/arm
    r={"source":read(d/"source"),"digests":read(d/"source-digests"),"cmd":{}}
    result[arm]=r
    if r["source"]!=os.environ["OSTOCK" if arm=="stock" else "OFIXED"] or not r["digests"] or not all((d/k).is_file() and not read(d/k) for k in ("before-status","after-status")):
        problems.append(arm+": missing source proof")
    for cmd in (("unit","owners") if arm=="owners" else (arm,)):
        meta={k:read(d/(cmd+"."+k)) for k in ("start","end","exit","argv")};r["cmd"][cmd]=meta
        if meta["exit"]!=("3" if arm=="stock" else "0") or not meta["start"] or not meta["end"]:problems.append(arm+"/"+cmd+": original status or clock")
    if arm=="owners":
        for target in ("gofer_test","transport_test","gofer_nogo","transport_nogo","uds_nogo"):
            rows=cases(d/(target+".test.xml"))
            r[target]={"cases":len(rows),"failures":[t for t in rows if t["failed"]],"skips":[t["name"] for t in rows if t["skipped"]]}
            if target == "uds_nogo":
                r[target]["reused_from"] = read(d/"uds_nogo.source.txt")
                if not r[target]["reused_from"]:
                    problems.append("missing original host fixture analyzer source")
            if not rows or r[target]["failures"]:problems.append(arm+"/"+target+": XML")
            if target.endswith("nogo"):
                for arch in ("arm64","amd64"):
                    if not read(d/(target+"."+arch+".findings.sha256")):problems.append(target+"/"+arch+": missing analyzer findings")
        log=read(d/"transport_test.test.log")
        for t in ("TestSCMSenderBeforeSaveIdempotent","TestSeveredRecvSend"):
            if "--- PASS: "+t+" " not in log:problems.append("owning regression missing: "+t)
        continue
    rows=cases(d/"guest.test.xml");log=read(d/"guest.test.log")
    r["xml"]=rows
    r["failure_context"]=[line for line in log.splitlines() if any(s in line for s in ("Failure","EPOLLOUT","Actual:","Expected:","Value of: ready"))]
    run="[ RUN      ] "+name
    outcome=("[  FAILED  ] " if arm=="stock" else "[       OK ] ")+name
    if log.count(run)!=1 or outcome not in log or "[  SKIPPED ] "+name in log or not rows or any(t["skipped"] for t in rows) or (arm=="stock")!=any(t["failed"] for t in rows):
        problems.append(arm+": named case status")
    if arm=="stock":
        section=log.partition(run)[2].partition("[  FAILED  ]")[0]
        if section.count("Failure")!=1 or "Value of: ready" not in section or "Actual: 0" not in section:
            problems.append(arm+": expected only timeout after confirmed drain")
if all(result[a]["digests"] for a in ("native","stock","fixed")):
    if len({tuple(result[a]["digests"].splitlines()[:2]) for a in ("native","stock","fixed")})!=1:
        problems.append("different syscall fixture")
out={"ok":not problems,"verified_at":datetime.now(timezone.utc).isoformat(),"errors":problems,"arms":result}
(root/"verdict.json").write_text(json.dumps(out,indent=2)+"\n")
print(json.dumps({"ok":not problems,"errors":problems}))
raise SystemExit(bool(problems))
