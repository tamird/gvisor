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
names=("TestStatefileOutputParentSymlink/argument","TestStatefileOutputParentSymlink/link_target","TestStatefileOutputParentSymlink/working_directory")
arms={}
errors=[]
for arm in ("stock","partial","proposed"):
    d=root/arm
    try:
        v=json.loads((d/"verdict.json").read_text())
        logs="\n".join(p.read_text() for p in (d/"original").rglob("test.log"))
        assert not v["errors"] and not v["missing"] and v["executed"] and set(v["expected_labels"])=={"//runsc/cmd:cmd_test"}
        assert v["native_exit"]==(0 if arm=="proposed" else 3)
        for name in names:
            expected=("FAIL" if arm=="stock" or (arm=="partial" and name.endswith("/working_directory")) else "PASS")
            assert re.search(r"(?m)^\s*--- "+expected+r": "+re.escape(name)+r" \(",logs),name
        if arm!="proposed":
            assert logs.count('checkpoint changed: got "v\\n"')==(3 if arm=="stock" else 1), "wrong count of input overwrites"
        else:
            assert not v["skips"] and v["case_counts"].get("failed",0)==0
        arms[arm]={k:v[k] for k in ("source","native_exit","case_counts","skips","execution_finished_at","verdict_available_at")}
    except (AssertionError,KeyError,OSError,ValueError) as exc:
        errors.append(f"{arm}: {exc}")
res=dict(arms=arms,errors=errors,status="incomplete" if errors else "verified",verified=datetime.now(timezone.utc).isoformat())
(root/"verdict.json").write_text(json.dumps(res,indent=2)+"\n")
print(json.dumps(res,indent=2))
sys.exit(bool(errors))
