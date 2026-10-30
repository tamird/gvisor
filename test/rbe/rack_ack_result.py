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
names=("TestGraphRecursiveDescriptor","TestLoadValueSnapshot","TestGraphDeferredMapKeys","TestGraphChecklistMapValue")
arms={}
errors=[]
for arm in ("stock","proposed"):
    d=root/arm
    try:
        verdict=json.loads((d/"verdict.json").read_text())
        log="\n".join(p.read_text() for p in (d/"original").rglob("test.log"))
        outcomes={}
        for name in names:
            hits=re.findall(r"(?m)^--- (PASS|FAIL): "+name+r" \(", log)
            assert hits and len(set(hits))==1, (name,hits)
            outcomes[name]=hits[0]
        assert verdict["native_exit"] in (0,3) and not verdict["missing"], verdict
        assert verdict["executed"] and set(verdict["expected_labels"])=={"//pkg/state:state_test"}
        assert not verdict["errors"], verdict["errors"]
        if arm=="stock" and any(v!="FAIL" for v in outcomes.values()):
            raise ValueError(("original failure not reproduced",outcomes))
        arms[arm]=dict(source=verdict["source"],outcomes=outcomes,native_exit=verdict["native_exit"],finished=verdict["execution_finished_at"],verdict=verdict["verdict_available_at"])
    except (AssertionError,ValueError,KeyError,OSError) as exc:
        errors.append(f"{arm}: {exc}")
result=dict(status="incomplete" if errors else "verified",arms=arms,errors=errors,verified=datetime.now(timezone.utc).isoformat())
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result,indent=2))
sys.exit(bool(errors))
