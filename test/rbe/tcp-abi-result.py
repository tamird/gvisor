"""Read actual named GTest assertions and preserve independent lane outcomes."""
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as XML

root = Path(sys.argv[1])
expected={f"AllInetTests/TcpSocketTest.{n}/{i}" for n in ("NoLingerOnProcessExit","AbortiveLingerOnExit") for i in range(2)}
if len(sys.argv)==3:
    import datetime, os
    select="//pkg/sentry/socket/hostinet:hostinet_nogo"
    source=os.environ["FIXED"]
    start=(root/"analyzer-start").read_text().strip()
    end=(root/"analyzer-end").read_text().strip()
    rc=int((root/"analyzer-exit").read_text())
    bep=root.parent/"analyzer.bep"
    targets={}; tests=[]; finished=[]
    if bep.is_file():
        for line in bep.open():
            try: e=json.loads(line)
            except ValueError: continue
            kind=e.get("id",{})
            if label:=kind.get("targetCompleted",{}).get("label"):
                if label==select: targets[label]=e.get("completed",{}).get("success",False)
            if kind.get("testSummary",{}).get("label")==select:
                tests.append(e.get("testSummary",{}).get("overallStatus"))
            if "buildFinished" in kind:
                finished.append(e.get("finished",{}).get("exitCode",{}).get("name"))
    passed=rc==0 and targets.get(select) and tests==["PASSED"] and finished==["SUCCESS"]
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    out=dict(source=source,start=start,completed=end,verified_at=now,exit=rc,target=select,completed_targets=targets,tests=tests,finished=finished,verified=bool(passed),invocation="f3a8f67a-0d40-4692-ab0f-1804bac5356b")
    (root/"analyzer.json").write_text(json.dumps(out,indent=2)+"\n")
    sys.exit(not passed)
result = {}
for name in ("netstack",):
    directory = root/name
    status = int((directory/"exit").read_text()) if (directory/"exit").is_file() else None
    assertions = {}
    if (directory/"test.xml").is_file():
        for case in XML.parse(directory/"test.xml").iter("testcase"):
            test = case.get("classname","")+"."+case.get("name","")
            failures = case.findall("failure")+case.findall("error")
            assertions[test] = {
                "status": "SKIP" if case.find("skipped") is not None or case.get("status")=="notrun"
                          else "FAIL" if failures else "OK",
                "details": [node.text or node.get("message","") for node in failures],
            }
    verified = set(assertions) == expected and status == 0 and all(a["status"] == "OK" for a in assertions.values())
    import datetime, os, re
    source=(directory/"source").read_text().strip() if (directory/"source").is_file() else ""
    verified &= source == os.environ["FIXED"]
    log=(directory/"bazel.log").read_text(errors="replace") if (directory/"bazel.log").is_file() else ""
    diagnostics=[s[:360] for line in log.splitlines() if (s:=line.strip()).startswith(("error:", "ERROR:", "Error:")) and not any(k in s.lower() for k in ("header", "key=", "token"))][:8]
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    result[name]=dict(source=source,expected=sorted(expected),executed=sorted(assertions),outcomes=assertions,missing=sorted(expected-assertions.keys()),exit=status,verified=verified,completed=(directory/"end").read_text().strip() if (directory/"end").is_file() else None,verified_at=now,diagnostics=diagnostics,fixture=(directory/"fixture-sha256").read_text().split()[0] if (directory/"fixture-sha256").is_file() else None,invocations=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})",log))))
result["verified"]=all(a["verified"] for a in result.values()) and len({a["fixture"] for a in result.values()})==1
result["selected_label"]=(root/"selected-label").read_text().strip() if (root/"selected-label").is_file() else ""
result["verified"] &= result["selected_label"]=="//test/syscalls:tcp_socket_test_runsc_systrap_directfs"
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
sys.exit(not result["verified"])
