"""Compare runtime frame filenames using the same native Go input and release transition."""

import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import sys
from pathlib import Path

SOURCE = 'bb983b988802b854bd03dd7a0a5220e7e7b4440e'
PACKAGE = '//test/rbe/generated_go_paths'
DIRECT = [
    '--config=aarch64',
    '--platforms=@llvm//platforms:linux_aarch64_glibc_static',
    '--host_platform=//test/rbe/release_prewarmer:arm64',
    '--extra_execution_platforms=//test/rbe/release_prewarmer:arm64',
    '--extra_toolchains=//test/rbe/go_bootstrap:linux_arm64_ash',
]
FLAGS = [
    '-c', 'opt', '--jobs=4', '--lockfile_mode=off', '--color=no', '--curses=no',
    '--noshow_progress', '--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain',
]
RESULT = Path(os.environ['RUNNER_TEMP']) / 'go-paths' / 'result.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def sample(mode, treatment, row):
    target = PACKAGE + (':probe' if mode == 'direct' else ':paired')
    flags = [*FLAGS, *(DIRECT if mode == 'direct' else ['--config=x86_64'])]
    if treatment == 'strip':
        flags.append('--experimental_output_paths=strip')
    subprocess.run([sys.argv[1], 'build', *flags, target], check=True)
    row['built'] = True
    files = output(sys.argv[1], 'cquery', *flags, '--output=files',
                   f'filter("{PACKAGE}:probe$", deps({target}))').splitlines()
    [binary] = map(Path, files)
    assert binary.parts[0] == 'bazel-out' and 'bin' in binary.parts
    with binary.open('rb') as stream:
        header = stream.read(20)
    assert header[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', header, 18)[0] == 183
    row.update(binary_sha256=sha(binary), configuration=binary.parts[binary.parts.index('bin') - 1])
    frames = json.loads(output(str(binary)))
    observations = {}
    root = Path(*binary.parts[:binary.parts.index('bin') + 1])
    for kind, method in [('checked_in', 'Load'), ('generated', 'StateTypeName')]:
        frame = frames[kind]
        assert frame['Function'] == f'gvisor.dev/gvisor/pkg/atomicbitops.(*Int32).{method}'
        filename = frame['File']
        assert re.fullmatch(r'(?:[a-zA-Z0-9_+./-]+/)?pkg/atomicbitops/[a-zA-Z0-9_]+\.go', filename)
        assert type(frame['Line']) is int and frame['Line'] > 0
        reported = Path(filename)
        short = Path('pkg/atomicbitops') / reported.name
        actual = short if kind == 'checked_in' else root / short
        if kind == 'checked_in':
            assert filename == 'pkg/atomicbitops/32b_64bit.go'
        else:
            assert reported.name.endswith('_state_autogen.go')
        source = actual.read_text()
        assert f') {method}(' in source
        observations[kind] = {
            'file': filename, 'line': frame['Line'], 'function': frame['Function'],
            'source_sha256': sha(actual),
            'readable_as_reported': reported.is_file() and sha(reported) == sha(actual),
        }
        if kind == 'generated':
            alias = Path('bazel-bin') / short
            observations[kind]['bazel_bin_matches'] = alias.is_file() and sha(alias) == sha(actual)
    row['frames'] = observations


def main():
    assert output('git', 'rev-parse', 'HEAD^') == SOURCE and platform.machine() == 'aarch64'
    result = {'source': SOURCE, 'host': platform.machine(), 'cases': {}, 'comparison': {}}
    for treatment in ['default', 'strip']:
        table = result['cases'][treatment] = {}
        for mode in ['direct', 'paired']:
            row = table[mode] = {'built': False}
            try:
                sample(mode, treatment, row)
                row['complete'] = True
            except (AssertionError, OSError, ValueError, subprocess.CalledProcessError) as error:
                row.update(complete=False, error=type(error).__name__)
                print(f'{treatment}/{mode}: {error}', file=sys.stderr, flush=True)
            finally:
                RESULT.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
        if all(row['complete'] for row in table.values()):
            direct, paired = table['direct'], table['paired']
            result['comparison'][treatment] = {
                'different_bazel_configurations': direct['configuration'] != paired['configuration'],
                'equal_checked_in_frame': direct['frames']['checked_in'] == paired['frames']['checked_in'],
                'equal_generated_bytes': direct['frames']['generated']['source_sha256'] == paired['frames']['generated']['source_sha256'],
                'equal_generated_filename': direct['frames']['generated']['file'] == paired['frames']['generated']['file'],
                'equal_generated_line': direct['frames']['generated']['line'] == paired['frames']['generated']['line'],
                'equal_binary': direct['binary_sha256'] == paired['binary_sha256'],
            }
        RESULT.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    assert set(result['comparison']) == {'default', 'strip'}
    for comparison in result['comparison'].values():
        assert comparison['different_bazel_configurations'] and comparison['equal_generated_bytes']
        assert comparison['equal_checked_in_frame'] and comparison['equal_generated_line']
    assert not result['comparison']['default']['equal_generated_filename']


if __name__ == '__main__':
    main()
