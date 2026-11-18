"""Freeze the canonical VFIO syscall runner and complete opaque release."""
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

stage, results = map(Path, sys.argv[1:])
stage.mkdir()
paths = set()
for platform in ('native', 'runsc_systrap'):
    wrapper = Path('bazel-bin/test/syscalls/vfio_pci_device_test_' + platform)
    text = wrapper.read_text()
    commands = [shlex.split(line) for line in text.splitlines() if line.startswith('exec ')]
    if len(commands) != 1:
        raise SystemExit('Expected one canonical wrapper exec')
    args = commands[0]
    runner, test = args[1], args[-1]
    if not re.fullmatch(r'test/runner/[A-Za-z0-9_./-]+', runner) or not re.fullmatch(r'test/syscalls/linux/[A-Za-z0-9_./-]+', test):
        raise SystemExit('Unexpected canonical wrapper executable paths')
    if any('..' in Path(path).parts for path in (runner, test)):
        raise SystemExit('Unsafe wrapper path')
    paths.update((runner, test))
    shutil.copyfile(wrapper, stage / platform)
    (stage / platform).chmod(0o755)
paths.add('test/runner/setup_container/setup_container')
for path in sorted(paths):
    target = stage / path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path('bazel-bin') / path, target)
    target.chmod(0o755)
# Dereference the complete release in one operation, without selecting its members.
shutil.copytree(Path('bazel-bin/release').resolve(), stage / 'release', symlinks=False)
ldd = []
libs = set()
for path in sorted(paths):
    binary = stage / path
    result = subprocess.run(['ldd', str(binary)], capture_output=True, text=True, timeout=20)
    output = result.stdout + result.stderr
    ldd.append({'binary': path, 'status': result.returncode, 'output': output})
    if 'not found' in output:
        raise SystemExit('Unresolved runtime library')
    if result.returncode:
        description = subprocess.check_output(['file', str(binary)], text=True, timeout=10)
        if 'statically linked' not in description and 'static-pie linked' not in description:
            raise SystemExit('Unrecognized executable linkage')
        continue
    for line in output.splitlines():
        words = line.split()
        name = words[2] if len(words) > 2 and words[1] == '=>' else words[0] if words else ''
        if name.startswith('/'):
            if not name.startswith(('/lib/', '/lib64/', '/usr/lib/')) or not Path(name).is_file():
                raise SystemExit('Unexpected runtime library path')
            libs.add(name)
(results / 'runtime-library-evidence.json').write_text(json.dumps(ldd, indent=2) + '\n')
# Preserve the exact loader and shared dependencies for this guest only.
with tarfile.open(stage / 'runtime-libs.tar', 'w', dereference=True) as archive:
    for name in sorted(libs):
        archive.add(name, arcname=name.lstrip('/'), recursive=False)
(stage / 'case-name').write_text('Open\n')
manifest = []
for path in sorted(stage.rglob('*')):
    if path.is_symlink() or (not path.is_file() and not path.is_dir()):
        raise SystemExit('Nonregular suite member')
    if path.is_file():
        manifest.append({'path': path.relative_to(stage).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': path.stat().st_mode & 0o777})
(stage / 'sha256').write_text(''.join(f"{item['sha256']}  {item['path']}\n" for item in manifest))
(results / 'product-fileset.json').write_text(json.dumps(manifest, indent=2) + '\n')
with tarfile.open(stage.with_suffix('.tar'), 'w') as archive:
    for path in sorted(stage.iterdir()):
        archive.add(path, arcname=path.name)
shutil.copyfile(stage.with_suffix('.tar'), results / 'product-suite.tar')
