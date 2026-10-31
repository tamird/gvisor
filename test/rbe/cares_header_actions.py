"""Verify the two header actions without repeating any completed build."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from zipfile import ZipFile

temp = Path(os.environ['RUNNER_TEMP'])
old = temp / 'cares-original.zip'
assert hashlib.sha256(old.read_bytes()).hexdigest() == 'e6079ee488282e0bd26d1f01648279877380fe1ddda3ec5caed01043725e4df6'
with ZipFile(old) as archive:
    original = json.loads(archive.read('verdict.json'))
source = '4e604a8c92797cd6830963aba7064d8c6f55ab20'
registry = Path('test/rbe/cares-registry-head').read_text().strip()
assert original['heads'] == {'fixture': source, 'registry': registry}
assert original['missing'] == original['skips'] == []
assert original['executed'] == original['expected'] == list(original['arms'])
assert original['failures'] == ['candidate-amd64', 'candidate-arm64']
for name, arm in original['arms'].items():
    assert arm['invocation']
    if name.startswith('candidate'):
        assert arm['bazel_exit'] == arm['header_without_client_utilities_exit'] == 0
        assert arm['target_results'] == {'ares_config_h': True, 'ares': True}
        assert arm['actions'] == []
        assert arm['header_sha256'] == '8d30eb7faf4f0b819153c476f01669a72754548e121659c73928378d12d37ea2'
    else:
        assert arm['status'] == 'pass' and arm['bazel_exit'] != 0
        assert arm['client_missing_cp'] and not any(arm['target_results'].values())
        assert len(arm['copy_actions']) == len(arm['targets'])

out = temp / 'publication'
out.mkdir(exist_ok=True)
filename = out / 'verdict.json'
def emit():
    original['failures'] = [name for name, arm in original['arms'].items() if arm['status'] != 'pass']
    original['status'] = 'incomplete_or_failed' if original['failures'] or original['missing'] else 'pass'
    original['collected'] = time.time()
    filename.write_text(json.dumps(original, indent=2) + '\n')
original['reconciliation'] = {'verifier': os.environ['GITHUB_SHA'], 'build_artifact': 13025534182, 'build_digest': hashlib.sha256(old.read_bytes()).hexdigest()}
emit()

# Use the original resolver and platforms. The credential stays in the
# coordinator's private temporary file, outside the artifact directory.
auth = temp / 'cares-query.bazelrc'
auth.touch(mode=0o600)
token = os.environ.pop('BUILDBUDDY_API_KEY')
auth.write_text('build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\n'
                'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io\n'
                f'build:buildbuddy_remote_executor --remote_header=x-buildbuddy-api-key={token}\n')
del token
bazel = ['bazelisk', '--bazelrc=test/rbe/qualification.bazelrc', '--bazelrc=' + str(auth)]
os.environ['USE_BAZEL_VERSION'] = Path('images/default/bazelversion').read_text().strip()
module = Path('MODULE.bazel')
contents = module.read_text()
label = '@@c-ares+//:ares_config_h'
try:
    module.write_text(contents + f'\nsingle_version_override(module_name = "c-ares", version = "1.34.9.bcr.1", registry = "https://raw.githubusercontent.com/tamird/bazel-central-registry/{registry}")\n')
    for arch, config, target in [('amd64', 'rbe', 'x86_64'), ('arm64', 'rbe-arm64', 'aarch64')]:
        arm = original['arms']['candidate-' + arch]
        # Ask Bazel for the named target's own actions. No output pathname regex is needed.
        args = [*bazel, 'aquery', '--config=' + config, '--config=' + target, '--lockfile_mode=update', '--output=jsonproto', label]
        graph = json.loads(subprocess.check_output(args))
        targets = {str(t['id']):t['label'] for t in graph['targets']}
        owned = [a for a in graph['actions'] if targets[str(a['targetId'])].endswith('//:ares_config_h')]
        arm['actions'] = [a['mnemonic'] for a in owned]
        print(arch, arm['actions'], flush=True)
        assert len(owned) == 1 and owned[0]['mnemonic'] == 'Symlink', arm['actions']
        assert len(owned[0]['outputIds']) == 1
        arm['status'] = 'pass'
        emit()
finally:
    module.write_text(contents)
    auth.unlink(missing_ok=True)
    emit()
