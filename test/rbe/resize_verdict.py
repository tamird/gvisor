import json,os,re,sys
from datetime import datetime,timezone
from pathlib import Path
from xml.etree import ElementTree

def verify(d):
    suite='TestRestoreCgroupMemoryLimit'
    expected={suite, suite+'AfterUpdate'}
    xml=d/'test.xml'
    records={}
    if xml.exists():
        for case in ElementTree.parse(xml).iter('testcase'):
            name=case.get('name')
            records[name]='FAIL' if case.find('failure') is not None or case.find('error') is not None else ('SKIP' if case.find('skipped') is not None else 'PASS')
    logs=(d/'test.log').read_text(errors='replace') if (d/'test.log').exists() else ''
    observations={}
    for name in sorted(expected):
        start=re.search(r'^=== RUN\s+'+name+r'\s*$',logs,re.M)
        vals={}
        if start:
            block=re.split(r'^=== RUN\s+',logs[start.end():],maxsplit=1,flags=re.M)[0]
            for label,expr in [('created',r'created memory.max=(\d+)'),('restore',r'restore memory.max=(\d+)'),('total',r'budget total=(\d+)'),('go_limit',r'go_limit=(\d+)'),('oom_kill',r'limits [^\n]*?oom_kill (\d+)')]:
                m=re.search(expr,block)
                if m:vals[label]=int(m.group(1))
        observations[name]=vals
    rc=int((d/'exit').read_text());source=(d/'source').read_text().strip()
    a,b=(observations[n] for n in (suite,suite+'AfterUpdate'))
    ok=(set(records)==expected and source==os.environ['SOURCE'] and records[suite]=='PASS' and a.get('restore')==a.get('total')==200<<20 and a.get('oom_kill')==0 and b.get('created')==2<<30 and b.get('restore')==200<<20 and b.get('total')==2<<30 and b.get('go_limit',0)>b['restore'] and a.get('go_limit',2**63)<a['restore'] and ((rc==0 and records[suite+'AfterUpdate']=='PASS') or (rc==3 and records[suite+'AfterUpdate']=='FAIL' and b.get('oom_kill',0)>0)))
    result=dict(verdict='STALE_LIMIT_AFTER_UPDATE' if ok else 'FAIL_OR_INCOMPLETE',source=source,expected=sorted(expected),executed=records,missing=sorted(expected-set(records)),observations=observations,bazel_exit=rc,execution_finished=(d/'end').read_text().strip(),verified=datetime.now(timezone.utc).isoformat())
    (d/'verdict.json').write_text(json.dumps(result,indent=2)+'\n');print(result);return ok
if __name__=='__main__':sys.exit(not verify(Path(sys.argv[1])))
