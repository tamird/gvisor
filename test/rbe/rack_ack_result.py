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
arms = {}
errors = []
for label in ('control', 'tests', 'analyze', 'race'):
    d=root/label
    if not (d/'verdict.json').exists():
        errors.append(f'{label}: no verdict'); continue
    v=json.loads((d/'verdict.json').read_text())
    logs='\n'.join(p.read_text() for p in (d/'original').rglob('test.log'))
    why=list(v['errors'])
    if v['missing'] or set(v['expected_labels'])!=set((d/'targets').read_text().splitlines()) or not v['executed']:
        why.append('missing or unexpected targets')
    if label=='control':
        if v['native_exit']!=3 or v['case_counts'].get('failed')!=1 or 'RACK RTT after combined ACK = 110ms, want 100ms' not in logs or 'RACK detected reordering before the missing packet was acknowledged' not in logs:
            why.append('expected both original RTT and reordering failures')
    elif v['status']!='PASS' or v['native_exit']!=0:
        why.append('corrected target failed')
    if label in ('tests','race') and '--- PASS: TestRACKDetectReorder' not in logs:
        why.append('corrected combined-ACK case did not run')
    if label=='race' and '--config=race' not in v['arguments']:
        why.append('missing race configuration')
    arms[label]={k:v[k] for k in ('source','native_exit','expected_labels','case_counts','missing','skips','execution_finished_at','verdict_available_at')}
    arms[label]['verification_errors']=why
    errors.extend(f'{label}: {w}' for w in why)
verified=datetime.now(timezone.utc).isoformat()
result=dict(status='incomplete' if errors else 'pass',arms=arms,errors=errors,verified=verified)
(root/'verdict.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
sys.exit(bool(errors))
