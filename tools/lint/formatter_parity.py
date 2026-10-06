"""Qualification-only comparison of the old and declared LLVM formatters."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from python.runfiles import runfiles

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--old', required=True)
p.add_argument('--new', required=True)
p.add_argument('--sources', required=True)
p.add_argument('--configs', required=True)
a = p.parse_args()
r = runfiles.Create()
assert r is not None
old, new = r.Rlocation(a.old), r.Rlocation(a.new)
assert old and new
sources = Path(r.Rlocation(a.sources))
configs = Path(r.Rlocation(a.configs))
names = json.loads(sources.read_text())
assert names
records = []
with tempfile.TemporaryDirectory(dir=os.environ['TEST_TMPDIR']) as directory:
    root = Path(directory)
    for manifest in (sources, configs):
        for name in json.loads(manifest.read_text()):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(manifest.parent / 'files' / (name + '.source'), target)
    assert (root / '.clang-format').is_file()
    for name in names:
        before = subprocess.run([old, name], cwd=root, stdout=subprocess.PIPE, check=True)
        after = subprocess.run([new, name], cwd=root, stdout=subprocess.PIPE, check=True)
        records.append({'path': name, 'equal': before.stdout == after.stdout,
                        'oldSha256': hashlib.sha256(before.stdout).hexdigest(),
                        'newSha256': hashlib.sha256(after.stdout).hexdigest()})
versions = {label: subprocess.check_output([tool, '--version'], text=True).strip()
            for label, tool in [('old', old), ('new', new)]}
assert '23.1.1' in versions['old'] and '23.1.2' in versions['new'], versions
result = {'versions': versions, 'files': records}
(Path(os.environ['TEST_UNDECLARED_OUTPUTS_DIR']) / 'formatter-parity.json').write_text(
    json.dumps(result, indent=2) + '\n')
mismatches = [record['path'] for record in records if not record['equal']]
assert not mismatches, mismatches
print(f'Identical formatter bytes for all {len(records)} indexed C/C++ files.')
