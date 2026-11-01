"""Verify the named syscall, restored socket, and original Bazel reports."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
name = "AllTCPSockets/TCPSocketPairTest.ReceiveAfterShutdownWrite"
expected = [name+"/0", name+"/1"]
report = {"base": os.environ["BASE"], "stock": os.environ["STOCK"], "fixed": os.environ["FIXED"], "families": {expected[0]: "IPv6", expected[1]: "IPv4"}, "arms": {}}
lines = tuple(map(int, os.environ["AFTER_LINES"].split(",")))
for arm in ["linux", "stock", "stock-restore", "fixed-restore"]:
    d = root/arm
    def read(f):
        p = d/f
        return p.read_text(errors="replace") if p.is_file() else ""
    rc = read("exit").strip()
    log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", read("test.log"))
    xml = read("test.xml")
    restored = arm.endswith("-restore")
    negative = arm == "stock-restore"
    cases = {}
    if xml:
        for c in ET.fromstring(xml).iter("testcase"):
            key = c.get("classname", "")+"."+c.get("name", "")
            failures = [n.text or n.get("message", "") for n in c.findall("failure")+c.findall("error")]
            cases[key] = {"result": "SKIP" if c.find("skipped") is not None or c.get("status") == "notrun" else "FAIL" if failures else "PASS", "failures": failures}
    wire = {k: {"restores": 0} for k in expected}
    current = None
    restore_re = re.compile(r"Executing:.*\srestore\s.*--image-path")
    for l in log.splitlines():
        for k in expected:
            if re.search(r"\[\s*RUN\s*\]\s+"+re.escape(k)+r"\b", l):
                current = k
            elif current == k and re.search(r"\[\s*(OK|FAILED|SKIPPED)\s*\]\s+"+re.escape(k)+r"\b", l):
                wire[k]["terminal"] = re.search(r"\[\s*(OK|FAILED|SKIPPED)\s*\]", l).group(1)
                current = None
        if current and restore_re.search(l):
            wire[current]["restores"] += 1
    evidence = []
    for item in log.splitlines():
        if restore_re.search(item):
            evidence.append("Executing: runsc restore --image-path <checkpoint>")
        elif name in item or "socket_ip_tcp_generic.cc:" in item or "Actual:" in item:
            if not re.search(r"secret|api.?key|authorization|remote_header", item, re.I):
                evidence.append(item[:450])
    if d.is_dir():
        (d/"trace.txt").write_text("\n".join(evidence)+"\n")
    path = read("target").strip()
    src = read("source").strip()
    failures = [t for c in cases.values() for t in c["failures"]]
    good = (set(cases) == set(expected) and all(c["result"] == ("FAIL" if negative else "PASS") for c in cases.values()) and rc == ("3" if negative else "0") and src == os.environ["FIXED" if arm=="fixed-restore" else "STOCK"] and path.endswith({"linux":"_native","stock":"_runsc_systrap","stock-restore":"_runsc_systrap_save","fixed-restore":"_runsc_systrap_save"}[arm]) and all((v["restores"]>0)==restored and v.get("terminal")==("FAILED" if negative else "OK") for v in wire.values()) and all(any("socket_ip_tcp_generic.cc:"+str(n) in t for n in lines) for t in failures))
    blog = read("bazel.log")
    errors = [s.strip()[:500] for s in blog.splitlines() if s.strip().startswith(("ERROR:", "error:", "Error:")) and not re.search("secret|password|api.?key|remote_header|authorization",s,re.I)][:8]
    excerpt = [s.strip()[:450] for s in log.splitlines() if ("socket_ip_tcp_generic.cc:" in s or "Actual:" in s or "Expected:" in s)][:16]
    xmlsha = hashlib.sha256(xml.encode()).hexdigest() if xml else None
    report["arms"][arm] = dict(verified=good,target=path,source=src,fixture=read("fixture-sha256").split()[:1],exit=rc,expected=expected,missing=sorted(set(expected)-set(cases)),cases=cases,wire=wire,log_assertions=excerpt,xml_sha256=xmlsha,started=read("start").strip(),finished=read("end").strip(),diagnostics=errors,invocations=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})",blog))))
report["analyzer"] = {"exit": (root/"analyzer-exit").read_text().strip() if (root/"analyzer-exit").is_file() else None, "targets":["//pkg/tcpip/transport/tcp:tcp_nogo"], "source":os.environ["FIXED"]}
report["verified"] = all(v["verified"] for v in report["arms"].values()) and report["analyzer"]["exit"] == "0" and len({str(v["fixture"]) for v in report["arms"].values()}) == 1
report["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
(root/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
print({n:(v["exit"],v["verified"],v["wire"],v["missing"]) for n,v in report["arms"].items()})
sys.exit(not report["verified"])
