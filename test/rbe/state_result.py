from datetime import datetime, timezone
from pathlib import Path
import json
import sys

mode, dirname, *rest = sys.argv[1:]
root = Path(dirname)
if mode == 'context':
    context = dict(expected_source=rest[0], commit_file='commit', source_before='before', source_after='after', final_head='head', executed_arguments='arguments', native_exit='exit', capture_exit='capture-exit', execution_finished_at=(root/'finish').read_text().strip(), command_kind='test')
    (root/'context.json').write_text(json.dumps(context)+'\n')
    sys.exit()

from xml.etree import ElementTree as ET
for arm in ('focused','proposed'):
    d=root/arm
    v=json.loads((d/'verdict.json').read_text())
    cases={c.get('name'):c for p in (d/'original').rglob('test.xml') for c in ET.parse(p).iter('testcase')}
    required=('TestNestedValueConversion/wire', 'TestNestedValueConversion/graph','TestBothEnginesStatefulCallbacks')
    assert v['source']=='62e214867e089f4ba2db0b79beecd8148c4da509' and not v['errors'] and not v['missing'] and v['native_exit']==0 and v['case_counts'].get('failed',0)==0, v
    assert set(v['expected_labels'])=={'//pkg/state/tests:tests_test'} and v['executed'], v
    if arm == "focused": assert all(n in cases and all(x.tag not in ('failure','skipped','error') for x in cases[n]) for n in required)
    print(json.dumps({k:v[k] for k in ('source','case_counts','execution_finished_at','verdict_available_at')}))
