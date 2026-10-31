from datetime import datetime, timezone
from pathlib import Path
import json
import sys

action, directory, *extra = sys.argv[1:]
root = Path(directory)
if action == 'context':
    ctx = dict(expected_source=extra[0], commit_file='commit', source_before='before', source_after='after', final_head='head', executed_arguments='arguments', native_exit='exit', capture_exit='capture-exit', execution_finished_at=(root/'finish').read_text().strip(), command_kind='build')
    (root/'context.json').write_text(json.dumps(ctx)+'\n')
    sys.exit()
errors = []
arms = {}
for arm in ('stock','named-amd64','named-arm64','no-label'):
    try:
        d = root/arm
        v = json.loads((d/'verdict.json').read_text())
        assert not v['errors'] and not v['missing'] and set(v['expected_labels']) == {'//runsc:runsc','//tools/bazeldefs:version'}, v.get('errors')
        assert v['native_exit'] == 0 and (d/'verifier-exit').read_text().strip() == '0'
        assert (d/'smoke').read_text().strip() == '0'
        label = '' if arm == 'no-label' else 'release-20991231.7'
        pkg = '' if arm == 'no-label' else '20991231.7'
        version = (d/'cli').read_text().splitlines()[0]
        assert version == 'runsc version '+label, repr(version)
        assert (d/'version.txt').read_bytes() == (pkg+'\n').encode()
        elf = (d/'file').read_text()
        assert ('ARM aarch64' if arm=='named-arm64' else 'x86-64') in elf, elf
        arms[arm] = dict(source=v['source'],labels=v['expected_labels'],version=version,package_version=pkg,execution_finished_at=v['execution_finished_at'],verdict_available_at=v['verdict_available_at'])
    except (OSError,AssertionError,KeyError,IndexError,ValueError) as exc:
        errors.append(f'{arm}: {type(exc).__name__}: {exc}')
report = dict(arms=arms,errors=errors,status='incomplete' if errors else 'verified',verified=datetime.now(timezone.utc).isoformat())
(root/'verdict.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
sys.exit(bool(errors))
