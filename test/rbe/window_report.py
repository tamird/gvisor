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
            if arm=='stock-build':
                hashes=get('runtime-identity').splitlines();verdict['stockRuntime']={k:s.split()[0] for k,s in zip(('runsc','sentry'),hashes,strict=True)}
            if arm=='nginx':
                log=get('nginx.test.log'); ids={}; rows=[];seq=[]; pair=role=current=None
                for ln in log.splitlines():
                    if not ln.startswith('SERVICE-'):continue
                    bits=ln.split();kv=dict(x.split('=',1) for x in bits[1:])
                    if bits[0]=='SERVICE-IDENT':ids[kv.pop('arm')]=kv
                    if bits[0]=='SERVICE-ARM':pair,role=int(kv['pair']),kv['arm'];seq.append((pair,role))
                    if bits[0]=='SERVICE-TRIAL':current={'pair':pair,'arm':role,'id':kv.pop('id'),'exe':kv.pop('exe'),**{k:int(v) for k,v in kv.items()}};rows.append(current)
                    if bits[0]=='SERVICE-KERNEL':current.update({k:int(v) for k,v in kv.items()})
                verdict.update(identities=ids,rows=rows,tail=[l[:320] for l in log.splitlines()[-27:]] if 'SERVICE-END pairs=24 warmups=2' not in log else [])
                want=[(i,role) for i in range(-1,24) for role in (('stock','candidate') if i%2==0 else ('candidate','stock'))]
                guard(seq==want and [(r['pair'],r['arm']) for r in rows]==want,'missing or duplicate live samples')
                guard(len({r['id'] for r in rows})==50 and len({r['pid'] for r in rows})==50,'container or Sentry process reused')
                guard(ids['stock']==verdict['stockRuntime'] and ids['candidate']['sentry']!=ids['stock']['sentry'],'release tree identity mismatch')
                guard('SERVICE-END pairs=24 warmups=2' in log,'missing completion')
                for r in rows:
                    guard(r['state']>0 and r['pagesMeta']>0 and r['workers']==4 and r['body']==1024 and r['http']==200 and 0<r['restore']<=r['ready'] and 0<r.get('cpuid',0)<r['restore'] and 0<r.get('kernel',0)<r['restore'],'invalid live measurements '+str(r))
                # SandboxPid is the runtime's OCI state PID. Require its executable
                # to match the selected release before reporting process residency.
                verdict['hwmVerified']=bool(rows) and all(r['exe']==ids[r['arm']]['sentry'] and r['highwater']>0 for r in rows)
                for role in ('stock','candidate'):
                    out=[]
                    for r in rows:
                        if r['arm']!=role or r['pair']<0:continue
                        for name in ('ready','restore','kernel','cpuid'):
                            if name in r:out.append(f"BenchmarkLive/{name} 1 {r[name]} ns/op\n")
                        if verdict['hwmVerified']:out.append(f"BenchmarkLive/highwater 1 {r['highwater']} peak-RSS-B/op\n")
                    (root/(role+'.bench')).write_text(''.join(out))
        except Exception as exc:errors.append(f'{arm}: {exc!r}')
    verdict.update(verified=datetime.now(timezone.utc).isoformat(),passed=not errors);(root/'verdict.json').write_text(json.dumps(verdict,indent=1)+'\n');print('qualified',not errors,'live trials',len(verdict['rows']),'errors',errors[:3]);
    if errors:raise SystemExit(1)

if __name__=='__main__':main(Path(sys.argv[1]))
