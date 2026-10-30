"""Reconcile the named C++ syscall cases, including skips, with Bazel exits."""
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as XML

p=Path(sys.argv[1])
expected={f"AllTCPSockets/TCPSocketPairTest.{name}/{i}" for i in range(12)
          for name in ("TCPCork", "TCPCorkFlushOnClose", "TCPCorkFlushOnShutdown")}
pattern=re.compile(r"\[\s*(OK|FAILED|SKIPPED)\s*\]\s+(AllTCPSockets/TCPSocketPairTest\.TCPCork(?:FlushOnClose|FlushOnShutdown)?/[0-9]+)(?=\s|$)")
verdict={}
for arm in ("linux","stock","proposed"):
    d=p/arm
    statuses={}
    for n in ("test.log","bazel.log"):
        if not (d/n).is_file():continue
        for status,name in pattern.findall((d/n).read_text(errors="replace")):
            statuses[name]=status
    xml={}
    if (d/"test.xml").is_file():
        for case in XML.parse(d/"test.xml").findall(".//testcase"):
            name=case.get("classname","")+"."+case.get("name","")
            if name in expected:
                xml[name]=("SKIPPED" if case.find("skipped") is not None else
                           "FAILED" if case.find("failure") is not None or case.find("error") is not None else "OK")
    native_exit=int((d/"exit").read_text().strip()) if (d/"exit").is_file() else None
    desired={n:("FAILED" if arm=="stock" and "TCPCorkFlush" in n else "OK") for n in expected}
    valid=native_exit==(3 if arm=="stock" else 0) and statuses==desired and not any(xml[k]!=statuses.get(k) for k in xml)
    reason=[line.strip() for line in (d/"test.log").read_text(errors="replace").splitlines() if "FIN never flushed" in line or "Neither receiver" in line] if (d/"test.log").is_file() else []
    verdict[arm]=dict(valid=valid,exit=native_exit,cases=statuses,xml=xml,missing=sorted(expected-statuses.keys()),failure_diagnostics=reason)
verdict["verified"]=all(v["valid"] for v in verdict.values())
(p/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
if not verdict["verified"]:raise SystemExit(1)
