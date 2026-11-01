"""Retain bounded observations for both original packet test binaries."""
import hashlib
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

root = Path(os.environ['RUNNER_TEMP'])
label = '//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test'
arms = {}
for arm in ('master', 'measured'):
    d = root / 'records' / arm
    log = (d / 'log').read_text(errors='replace')
    testlog = (d / 'test.log').read_text(errors='replace')
    records = []
    be = root / (arm + '.bep')
    if be.exists():
        for line in be.open():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    cases = []
    try:
        cases = [{'name': c.get('name'),'status': [child.tag for child in c if child.tag in ('failure','error','skipped')]} for c in ET.parse(d / 'test.xml').iter('testcase')]
    except ET.ParseError:
        pass
    matches=[s.strip() for s in testlog.splitlines() if 'WIRE schedule=' in s]
    pre=[s.strip() for s in testlog.splitlines() if 'BEFORE_RTO repairs=' in s]
    expected=['head-lost','fresh-new-sack','fresh-repeat-sack']
    result=[r['testResult'].get('status') for r in records if r['id'].get('testResult',{}).get('label')==label]
    note=[s.strip()[-400:] for s in (testlog+'\n'+log).splitlines() if re.search(r'tcp_rack_test\.go:\d+|^ERROR:|panic:|^--- FAIL',s) and not re.search('token|authorization|api[_-]?key|bearer|remote[_-]?header|password',s,re.I)][-15:]
    owner=[r['testSummary'].get('overallStatus') for r in records if r['id'].get('testSummary',{}).get('label')==label]
    ok=(int((d/'exit').read_text())==0 and result==owner==['PASSED'] and all(any(row['name'].endswith(sub) and not row['status'] for row in cases) and sum('WIRE schedule='+sub+' ' in s for s in matches)==1 for sub in expected))
    arms[arm]=dict(source=(d/'head').read_text().strip(),exit=int((d/'exit').read_text()),passed=ok,observations=matches,pre=pre,xml=cases,action=result,summary=owner,diagnostics=note if not ok else [],invocations=re.findall(r'Invocation ID: ([\w-]+)',log),log_digest=hashlib.sha256((d/'test.log').read_bytes()).hexdigest())
out={'fixture_sha256':hashlib.sha256((root/'rack-fresh-head.go.txt').read_bytes()).hexdigest(),'arms':arms,'complete':all(r['passed'] for r in arms.values())}
(root/'records/verdict.json').write_text(json.dumps(out,indent=2)+'\n')
raise SystemExit(not out['complete'])
