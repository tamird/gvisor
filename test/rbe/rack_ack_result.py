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
import re
names=("readonly_existing_file_rejected_when_unprivileged","dirs_without_read_perm_succeed_when_unprivileged")
arms={}
errors=[]
for arm in ("split",):
    try:
        d=root/arm
        v=json.loads((d/"verdict.json").read_text())
        logs="\n".join(p.read_text() for p in (d/"original").rglob("test.log"))
        labels=["//runsc/cmd:cmd_test","//runsc/cmd:statefile_output_permissions_test"]
        assert not v["errors"] and not v["missing"] and set(v["expected_labels"])==set(labels)
        assert v["native_exit"]==0 and not v["skips"], v
        for name in names:
            assert re.search(r"(?m)^\s*--- PASS: TestStatefileOutputPermissions/"+name+r" \(",logs), name
        symlinks=re.findall(r"(?m)^\s*--- PASS: TestStatefileOutputSymlinks/([^ ]+) \(",logs)
        assert len(set(symlinks))==8, symlinks
        arms[arm]={k:v[k] for k in ("source","native_exit","case_counts","skips","execution_finished_at","verdict_available_at")}
    except (KeyError,AssertionError,OSError,ValueError) as e:
        errors.append(f"{arm}: {e}")
result=dict(arms=arms,errors=errors,status="incomplete" if errors else "verified",verified=datetime.now(timezone.utc).isoformat())
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
sys.exit(bool(errors))
