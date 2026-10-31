"""Verify native, expected-stock and corrected syscall cases, then the owning targets."""
from datetime import datetime, timezone
import json, os, re, sys
from pathlib import Path
from xml.etree import ElementTree as ET

root = Path(sys.argv[1])
required = {"FuseTest.PollBeforeMount", "FuseTest.EpollAcrossMount"}
problems = []
arms = {}
for arm in ("native","stock","fixed","owners"):
    d = root / arm
    def read(k):
        f=d/k
        return f.read_text(errors="replace").strip() if f.is_file() else ""
    def xml(k):
        try:
            t = ET.parse(d/k)
            return [{"name":v.get("name"),"class":v.get("classname"),"status":v.get("status"),"skip":v.find("skipped") is not None,"failure":v.find("failure") is not None or v.find("error") is not None} for v in t.iter("testcase") if v.get("status") != "notrun"]
        except (OSError,ET.ParseError): return []
    info={"source":read("source"),"source_hashes":read("source-digests"),"commands":{}}
    arms[arm]=info
    if info["source"]!=os.environ["OSTOCK" if arm=="stock" else "OFIXED"] or not info["source_hashes"] or not all((d/k).is_file() and not read(k) for k in ("before-status","after-status")):
        problems.append(arm+": provenance")
    for c in (("build","native") if arm=="native" else ("unit","owners") if arm=="owners" else (arm,)):
        v={k:read(c+"."+k) for k in ("start","end","exit")}; v["argv"]=read(c+".argv").splitlines()
        info["commands"][c]=v
        if v["exit"] != ("3" if c=="stock" else "0") or not v["start"] or not v["end"]:
            problems.append(arm+"/"+c+": original command exit/timestamps")
    if arm=="owners":
        info["findings"] = {p.name:p.read_text().split()[0] for p in d.glob("*.sha256") if p.read_text().strip()}
        for n in ("fuse_nogo","fuse_test_nogo"):
            if not all(n+"."+a+".findings.sha256" in info["findings"] for a in ("amd64","arm64")):
                problems.append(arm+"/"+n+": missing findings")
        for n in ("fuse_test","fuse_nogo","fuse_test_nogo"):
            cases=xml(n+".test.xml")
            info[n]={"cases":len(cases),"failed":[x for x in cases if x["failure"]],"skipped":[x["name"] for x in cases if x["skip"]]}
            if not cases or any(x["failure"] for x in cases):
                problems.append(arm+"/"+n+": missing/failing cases")
        if "TestEpollAfterAbort" not in read("fuse_test.test.log") or not re.search(r"--- PASS: TestEpollAfterAbort\b",read("fuse_test.test.log")):
            problems.append(arm+": prior aborted-connection test did not pass")
        continue
    guest=arm!="native"
    log=read("guest.test.log" if guest else "test.log")
    cases=xml("guest.test.xml" if guest else "test.xml")
    expected="FAILED" if arm=="stock" else "OK"
    got={}
    for n in sorted(required):
        got[n]={m:len(re.findall(r"(?m)^\[\s*"+m+r"\s*\]\s+"+re.escape(n)+r"(?:\s|$)",log)) for m in ("RUN","OK","FAILED","SKIPPED")}
        if got[n]["RUN"]!=1 or not got[n][expected] or got[n]["SKIPPED"] or got[n]["OK" if expected=="FAILED" else "FAILED"]:
            problems.append(arm+"/"+n+f": {got[n]}")
    info["named"]=got
    info["xml"]={"cases":len(cases),"failed":[x["name"] for x in cases if x["failure"]],"skipped":[x["name"] for x in cases if x["skip"]]}
    if not cases or ((arm=="stock")!=any(x["failure"] for x in cases)) or any(x["skip"] for x in cases):
        problems.append(arm+": original XML")
    if arm=="stock":
        # Keep each failure section. Native and fixed must meet the same named assertions.
        for n in sorted(required):
            run=re.search(r"\[\s*RUN\s*\]\s+"+re.escape(n)+r"\s*\n(.*?)\[\s*FAILED\s*\]",log,re.S)
            if not run or not re.search(r"EPERM|Operation not permitted|errno 1\b",run[1],re.I):
                problems.append(arm+"/"+n+": missing registration errno")
        info["assertions"]=[line.strip() for line in log.splitlines() if "not permitted" in line.lower() or "EPERM" in line or "Failure" in line]
    else:
        info["kernel"]=read("kernel") if arm=="native" else ""
out={"ok":not problems,"errors":problems,"arms":arms,"verified_at":datetime.now(timezone.utc).isoformat()}
(root/"verdict.json").write_text(json.dumps(out,indent=2)+"\n")
print(json.dumps({"ok":out["ok"],"errors":problems}))
sys.exit(bool(problems))
