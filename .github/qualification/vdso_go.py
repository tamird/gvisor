"""Collect the exact normal target and cross-ELF comparison, even on failure."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

tmp=Path(os.environ["RUNNER_TEMP"])/"vdso-checker"
tmp.mkdir(exist_ok=True)
source="f61f120296dfe1af832a6a30a7b6bd0d8b8cc0d7"
head=subprocess.check_output(["git","rev-parse","HEAD^"],text=True).strip()
assert head==source
report={"product":source,"owners":{},"verified":False}
out=tmp/"result.json"
def save():
    out.write_text(json.dumps(report,sort_keys=True))
save()
cmd=[str(tmp/"bazel"),"--nosystem_rc","--nohome_rc","test","--config=x86_64","--jobs=4","--keep_going","--nocache_test_results",
     "--flaky_test_attempts=1","--test_timeout=600","--build_event_json_file="+str(tmp/"events"),"//vdso:vdso_test","//vdso:checker_controls"]
with (tmp/"build.log").open("w") as f:report["exit"]=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT).returncode
save()
errors=[]
for line in (tmp/"build.log").read_text(errors="replace").splitlines():
    if (line.startswith("ERROR:") or "fatal error:" in line or "command not found" in line) and len(errors)<6:
        errors.append(line.strip()[:340])
report["errors"]=errors
if (tmp/"events").exists():
    for line in (tmp/"events").open():
        e=json.loads(line);i=e["id"]
        if "started" in e:report["invocation"]=e["started"].get("uuid")
        if "testResult" in i:
            owner=i["testResult"]["label"];t=e["testResult"];info=t.get("executionInfo",{})
            assert owner in {"//vdso:vdso_test","//vdso:checker_controls"}
            r=report["owners"][owner]={"status":t["status"],"cached":bool(t.get("cachedLocally") or info.get("cachedRemotely"))}
            log=Path("bazel-testlogs")/owner[2:].replace(":","/")/"test.log"
            if log.is_file():
                b=log.read_bytes();r["log"]=[len(b),hashlib.sha256(b).hexdigest()]
                text=b.decode(errors="replace")
                rows=[json.loads(s.removeprefix("CHECKER-RECEIPT ")) for s in text.splitlines() if s.startswith("CHECKER-RECEIPT ")]
                if rows:report["fixtures"]=rows[-1]
                if t["status"]!="PASSED":
                    r["diagnostic"]=[s[:300] for s in text.splitlines() if any(w in s for w in ("Error:","missing", "not found","permission denied","Traceback"))][:6]
            save()
r=report.get("fixtures",{})
report["verified"]=(report["exit"]==0 and len(report["owners"])==2 and all(t["status"]=="PASSED" and not t["cached"] for t in report["owners"].values()) and
    r.get("fail")==[] and set(r.get("arches",{}))=={"amd64","arm64"} and all(x["before_sha256"]==x["sha256"] and
       x["valid"]["go"]==0 and x["linked_data"]["go"] and x["debian_valid"]["exit"]==0 and x["debian_data"]["exit"]
       and x["boundary"]["go"] and x["next_page"]["go"]==0 for x in r["arches"].values()))
save()
