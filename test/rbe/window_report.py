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
    checkouts = {"stock-build":("STOCK",[]),"candidate-tests":("PRODUCT",["unit","suite","analyzer"]),"nginx":("GITHUB_SHA",["nginx"])}
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
                log=get('nginx.test.log');pair=role=None;capture=None;trials=[];lives=[];trace=[];sequence=[]
                for line in log.splitlines():
                    if line.startswith('STREAM-CAPTURE ') and capture is None:capture=line;verdict['capture']=line
                    if line.startswith('COLD-ARM '):
                        kv=dict(x.split('=') for x in line.split()[1:]);pair,role=int(kv['pair']),kv['arm'];sequence.append((pair,role))
                    if line.startswith('COLD-TRIAL '):
                        v=dict(x.split('=') for x in line.split()[1:]);digest=v.pop('digest');reused=v.pop('reused');guard(digest in (capture or '') and reused in ('true','false'),'changed capture or pool flag');row=dict(pair=pair,arm=role,reused=reused=='true',**{k:int(n) for k,n in v.items()});trials.append(row);guard(row['cpuidcap']==0 and row['small']<20 and row['large']>1000 and row['kernelcap']<20,'not a fresh process')
                    if line.startswith('COLD-HTTP '):lives.append(dict(x.split('=') for x in line.split()[1:]))
                    if line.startswith('COLD-LOG '):trace.append(dict(x.split('=') for x in line.split()[1:] if '=' in x))
                verdict.update(rows=trials,live_restores=lives,decoder_trace=trace,live_diagnostics=[x[-400:] for x in log.splitlines() if 'Docker restore:' in x or 'bad restored HTTP response' in x or 'failed to read' in x or 'no route to host' in x or 'connection refused' in x][:12])
                want=[(i,role) for i in range(64) for role in (('stock','candidate') if i%2==0 else ('candidate','stock'))]
                guard(sequence==want and [(r['pair'],r['arm']) for r in trials]==want and len({r['pid'] for r in trials})==128 and 'COLD-SAMPLES pairs=64' in log,'incomplete independent processes');guard(len(lives)==3 and len({r['container'] for r in lives})==3 and all(r['http']=='200' and r['bytes']=='1024' for r in lives),'three actual restored HTTP responses');groups={t['group'] for t in trace};guard(len(groups)==3,'three actual sentry traces');
                for g in groups:
                    roots_g=[t['root'] for t in trace if t['group']==g];guard(all(any(suffix in root for root in roots_g) for suffix in ('cpuid.FeatureSet','kernel.Kernel','privateMemoryFileMetadata')),'missing production graph in '+g)
        except (OSError,IndexError,ValueError,AttributeError,KeyError) as exc:errors.append(arm+': '+str(exc)[:240])
    try:
        verdict['stock_binary']=json.loads((root.parent/'phase-results/build-stock.json').read_text());guard(verdict['stock_binary'].get('source')==roots['STOCK'] and 'digest' in verdict['stock_binary'],'stock .bin identity')
    except (ValueError,OSError) as exc:errors.append(str(exc))
    for arm in ('stock','candidate'):
        bench=[]
        for r in verdict.get('rows',[]):
            if r['arm']!=arm:continue
            for key,name in [('both','cpuid_then_kernel'),('kernel','kernel_first_load')]:
                bench.append(f"BenchmarkFresh/{name} 1 {r[key+'wall']} ns/op {r[key+'cpu']} cpu-ns/op\n")
        bench.extend(f"BenchmarkFresh/allocations 1 {r['bytes']} B/op {r['allocs']} allocs/op\n" for r in verdict.get('rows',[]) if r['arm']==arm)
        (root/(arm+'.bench')).write_text(''.join(bench))
    verdict.update(verified=datetime.now(timezone.utc).isoformat(),passed=not errors);(root/'verdict.json').write_text(json.dumps(verdict,indent=1)+'\n');print('qualified',verdict['passed'],'fresh samples',len(verdict['rows']),'failures',errors)
    if errors:raise SystemExit(1)

if __name__=='__main__':main(Path(sys.argv[1]))
