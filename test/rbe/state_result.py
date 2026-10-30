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
import re
import subprocess

source="62e214867e089f4ba2db0b79beecd8148c4da509"
top={name for name in re.findall(r"func (Test[A-Za-z0-9_]+)\(",subprocess.check_output(["git","grep","-h","-E","^func Test[A-Za-z0-9_]+\\(",source,"--","pkg/state/tests/*_test.go"],text=True))}
assert len(top)>40
maps={"TestMapsEmpty","TestMapsFull"}
groups=["core"]+[mode+"-"+prefix for mode,prefixes in (("Empty",("plain","pointers","interfaces","interfacesToPointers")),("Full",("plain","pointers","interfaces","interfacesToPointer"))) for prefix in prefixes]
seen=set()
summary={}
for group in groups:
    d=root/group
    verdict=json.loads((d/'verdict.json').read_text())
    assert verdict['status']=='PASS' and verdict['source']==source and verdict['native_exit']==0 and not verdict['missing'] and not verdict['errors'], (group,verdict)
    records={c.get('name'):c for p in (d/'original').rglob('test.xml') for c in ET.parse(p).iter('testcase')}
    if group=='core':
        assert {n.split('/')[0] for n in records}==top-maps, sorted((top-maps)^{n.split('/')[0] for n in records})
        for name in ('TestNestedValueConversion/graph','TestNestedValueConversion/wire','TestBothEnginesStatefulCallbacks/graph','TestBothEnginesStatefulCallbacks/wire'):
            assert name in records and all(x.tag not in ('error','skipped','failure') for x in records[name])
    else:
        mode,prefix=group.split('-')
        owner='TestMaps'+mode
        expected={owner}|{f'{owner}/{prefix}{n}' for n in range(1176)}
        assert set(records)==expected, (group, sorted(expected-set(records))[:8], sorted(set(records)-expected)[:8])
        assert verdict['case_counts'].get('passed')==len(expected), (group,verdict['case_counts'])
    assert not ((seen & set(records))-maps)
    seen.update(records)
    summary[group]={k:verdict[k] for k in ('case_counts','execution_finished_at','verdict_available_at')}
(root/'groups.json').write_text(json.dumps({'source':source,'topLevel':len(top),'uniqueNamed':len(seen),'mapSubcases':9408,'groups':summary},indent=2)+'\n')
print((root/'groups.json').read_text())
