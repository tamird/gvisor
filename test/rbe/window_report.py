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
                log=get('nginx.test.log');pair=role=None
                for line in log.splitlines():
                    if line.startswith('STREAM-CAPTURE '): verdict['capture']=line
                    if line.startswith('TABLE-ARM '):
                        pair,role=line.split()[1:];pair=int(pair.split('=')[1]);role=role.split('=')[1]
                    if line.startswith('TABLE-TRIAL '):
                        cols=dict(kv.split('=') for kv in line.split()[1:]);mode=cols.pop('mode');digest=cols.pop('digest');guard(digest in verdict['capture'],'capture changed');verdict['rows'].append(dict(pair=pair,arm=role,mode=mode,**{k:int(v) for k,v in cols.items()}))
                    if line.startswith('TABLE-SIZES '):verdict.setdefault('sizes',[]).append(line)
                    if line.startswith('TABLE-DONE '):verdict.setdefault('done',[]).append((pair,role,line))
                guard(re.search(r'^TABLE-COMPLETE pairs=16 captures=1 worker=\d+$',log,re.M),'incomplete real benchmark')
        except (OSError,IndexError,ValueError,AttributeError,KeyError) as exc:errors.append(arm+': '+str(exc)[:240])
    try:
        verdict['stock_binary']=json.loads((root.parent/'phase-results/build-stock.json').read_text());guard(verdict['stock_binary'].get('source')==roots['STOCK'] and 'digest' in verdict['stock_binary'],'stock .bin identity')
    except (ValueError,OSError) as exc:errors.append(str(exc))
    rows=verdict['rows'];modes={'small_after_small','small_after_kernel','kernel_after_kernel','cpuid_then_kernel'};guard({(r['pair'],r['arm'],r['mode']) for r in rows}=={(p,a,m) for p in range(16) for a in ('candidate','stock') for m in modes},'incomplete samples');guard(len(verdict.get('sizes',[]))==32 and len(verdict.get('done',[]))==32,'missing size or recovery controls');guard(all('recovered=true' in line for _,_,line in verdict.get('done',[])),'invalid stream recovery failed')
    for arm in ('stock','candidate'):
        text=''.join('BenchmarkDecodeTable/'+r['mode']+' '+str(r['n'])+' '+str(r['wall']/r['n'])+' ns/op '+str(r['cpu']/r['n'])+' cpu-ns/op '+str(r['bytes']/r['n'])+' B/op '+str(r['allocs']/r['n'])+' allocs/op\n' for r in rows if r['arm']==arm);(root/(arm+'.bench')).write_text(text)
    verdict.update(verified=datetime.now(timezone.utc).isoformat(),passed=not errors);(root/'verdict.json').write_text(json.dumps(verdict,indent=1)+'\n');print('qualified',verdict['passed'],'samples',len(rows),'failures',errors)
    if errors:raise SystemExit(1)

if __name__=='__main__':main(Path(sys.argv[1]))
