import json
import sys
from datetime import datetime,timezone
from pathlib import Path

cmd, root, *rest = sys.argv[1:]; root=Path(root)
if cmd=='context':
    ctx=dict(expected_source=rest[0],commit_file='commit',source_before='before',source_after='after',final_head='head',executed_arguments='arguments',native_exit='exit',capture_exit='capture-exit',execution_finished_at=(root/'finish').read_text().strip(),command_kind='build')
    (root/'context.json').write_text(json.dumps(ctx)+'\n');sys.exit()
if cmd=='action':
    a, = [a for a in json.loads(Path(rest[0]).read_text()).get('actions', []) if a.get('mnemonic') == rest[1]]
    (root/'action.json').write_text(json.dumps({k:a[k] for k in ('mnemonic','arguments')})+'\n');sys.exit()
errors=[];arms={}
for arm in ('stock','direct'):
    d=root/arm
    try:
        v=json.loads((d/'verdict.json').read_text())
        assert v['source']==("b61651f9371f514411dbb4fc2bd367f860204546" if arm=='stock' else "bfc0d1dfb6edad51ac12b4d9bbdb55a1a59ec00d"),v['source']
        assert v['native_exit']==0 and not v['errors'] and not v['missing'] and v['expected_labels']==['//:go_export'],v.get('errors')
        assert (d/'verifier-exit').read_text().strip()=='0'
        entries=(d/'entries').read_text().splitlines()
        assert 'go.mod' in entries and 'runsc/fdparking/fdparking.c' in entries,entries[:6]
        act=json.loads((d/'action.json').read_text())
        assert act['mnemonic']==('Genrule' if arm=='stock' else 'GoExport'),act['mnemonic']
        if arm=='direct':
            assert act['arguments'][0].endswith('/assemble') and not any(a in ('/bin/bash','-c') for a in act['arguments']),act['arguments'][:3]
        arms[arm]=dict(source=v['source'],sha256=(d/'sha256').read_text().strip(),entries=len(entries),action=act['mnemonic'],execution_finished_at=v['execution_finished_at'],verdict_available_at=v['verdict_available_at'])
    except (OSError,ValueError,KeyError,AssertionError) as e:
        errors.append(f'{arm}: {type(e).__name__}: {e}')
if not errors and arms['stock']['sha256']!=arms['direct']['sha256']:
    errors.append('archive bytes differ')
j=dict(status='incomplete' if errors else 'verified',arms=arms,errors=errors,verdict_available_at=datetime.now(timezone.utc).isoformat());(root/'verdict.json').write_text(json.dumps(j,indent=2)+'\n');print(json.dumps(j,indent=2));sys.exit(bool(errors))
