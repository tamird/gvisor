import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from qualification_report import xml_cases

d=Path(sys.argv[1])
ok=True
report={}
base='//pkg/state/tests:tests_test'
unit='//pkg/state:state_test'
for lane in ('stock','fix'):
    p=d/lane
    targets=[base] if lane=='stock' else [base,unit]
    records={}
    for label in targets:
        path=p/(label.rsplit(':',1)[-1]+'.xml')
        if path.exists():
            records[label]=xml_cases(path)
    names={}
    if base in records:
        for e in ET.parse(p/'tests_test.xml').iter('testcase'):
            n=e.get('name','')
            if n.startswith('TestSignedZero/'):
                names[n]='skipped' if e.find('skipped') is not None else ('failed' if e.find('failure') is not None or e.find('error') is not None else 'passed')
    wanted={f'TestSignedZero/{engine}/{i}': ('failed' if lane=='stock' and engine=='wire' else 'passed') for engine in ('wire','graph') for i in range(4)}
    rc=int((p/'rc').read_text())
    tree=(p/'tree').read_text().strip()
    compare=(set(records)==set(targets) and all(names.get(n)==s for n,s in wanted.items()) and rc==(3 if lane=='stock' else 0) and tree==os.environ[lane.upper()+'_TREE'])
    if lane=='stock' and base in records:
        compare &= all(name.split('TestSignedZero',1)[-1] in ('','/wire',*(f'/wire/{i}' for i in range(4))) for name in records[base]['failures'])
    if lane=='fix':
        compare &= all(v['passed'] and not v['failed'] for v in records.values())
    report[lane]=dict(tree=tree,target_records=records,expected=targets,missing=sorted(set(targets)-set(records)),case_results=names,missing_cases=sorted(set(wanted)-set(names)),bazel_exit=rc,qualified=bool(compare))
    ok &= compare
report.update(product=os.environ['PRODUCT'],base=os.environ['BASE'],verdict='REPRODUCED_AND_FIXED' if ok else 'FAIL_OR_INCOMPLETE',execution_finished=(d/'end').read_text().strip(),verified=datetime.now(timezone.utc).isoformat(timespec='seconds'))
(d/'verdict.json').write_text(json.dumps(report,indent=2)+'\n')
print(report)
sys.exit(not ok)
