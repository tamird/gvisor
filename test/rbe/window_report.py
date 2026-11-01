"""Reconcile source, tested binaries, framed bytes, writer errors and actual Nginx archives."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from qualification_report import xml_cases

def main(root: Path) -> None:
    roots = {k:os.environ[k] for k in ("BASE","PRODUCT","STOCK","CANDIDATE","GITHUB_SHA")}
    checkouts = {"stock-build":("STOCK",[]),"nginx":("GITHUB_SHA",["nginx"])}
    verdict = {"sources":roots,"arms":{},"wires":{},"rows":[],"peers":[],"failures":[]}
    errors = verdict["failures"]
    def guard(ok, why):
        if not ok: errors.append(why)
    for arm,(source, owners) in checkouts.items():
        p = root/arm
        def get(name):
            return (p/name).read_text().strip()
        try:
            arm_report = {"source":get("source"), "exit":int(get("exit")),"ended":get("end"),"owners":{}}
            verdict["arms"][arm] = arm_report
            guard(arm_report["source"]==get("source-after")==roots[source] and not get("source-before-status") and not get("source-after-status"),arm+" source mismatch")
            guard(arm_report["exit"]==0,arm+" Bazel failed")
            ids=re.findall(r"Invocation ID: ([0-9a-f-]{36})",get("bazel-diagnostics"))
            if ids: arm_report["invocation"]=ids[-1]
            if arm_report["exit"]:
                diag=get("bazel-diagnostics")
                arm_report["diagnosis"]=[s[:360] for s in diag.splitlines() if re.search(r"[.]go:\d+|^ERROR:|FAILED:|failed:|did not declare|undefined:|not found",s) and not re.search(r"(?i)token|header|secret|authorization|--",s)][:12]
            for owner in owners:
                stats=xml_cases(p/f"{owner}.test.xml")
                arm_report["owners"][owner]={k:stats[k] for k in ("passed","failed","skipped")}
                guard(stats["passed"]>0 and not stats["failed"],arm+"/"+owner+" missing or failed")
            if arm=='nginx':
                log=get('nginx.test.log');cap=re.search(r'STREAM-CAPTURE bytes=(\d+) digest=([0-9a-f]{64})',log);row=re.search(r'^STUDY-RESULT (.+)$',log,re.M)
                if not cap or not row:errors.append('missing graph profile: '+str([s[:240] for s in log.splitlines() if 'FAIL' in s or 'fatal' in s][-4:]))
                else:
                    v=json.loads(row[1]);verdict['profile']=v;guard(list(v['digests'])==[cap[2]] and v['pids']==194 and v['counts']=={'phase-cpu':24,'phase-alloc':8,'sample':160,'heap':2},'missing fresh processes, wrong capture');guard(v['phase_complete'] and v['sample_ns']['kernel']>=250000000 and v['sample_ns']['cpuid']>=100000000,'missing phases or insufficient labeled CPU samples');guard(v['heap_bytes']>1000000,'missing allocation profiles')
        except (OSError,IndexError,ValueError,AttributeError,KeyError) as exc:errors.append(arm+': '+str(exc)[:240])
    verdict.update(verified=datetime.now(timezone.utc).isoformat(),passed=not errors);(root/'verdict.json').write_text(json.dumps(verdict,indent=1)+'\n');print('qualified',verdict['passed'],'failures',errors)
    if errors: raise SystemExit(1)

if __name__=='__main__':main(Path(sys.argv[1]))
