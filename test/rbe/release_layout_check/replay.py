"""Replay the layout verifier against the evidence from both original runners."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


SOURCE = '4a2b2a00efd0c7aa19e7b58c98d380e58b7db5c2'
ARCHIVES = {
    'amd64': ('2ffeefd2226aba22ce1c3066f521f464edfd065a3afe1892e64871544e586f9a', '71e61112-bbc7-4f84-a57b-88c7c6733349'),
    'arm64': ('2f271465f503de4a9d93926110c4cea28856402d40b1ae083b2016f00bd933a2', '1ca9c3ce-f1ef-4342-9f0d-439307a755e2'),
}
RUNTIMES = {
    'amd64': '94d15774d91c9c1acf089e2a44d873bffdaba75979610e1b8ae59c5afa485be0',
    'arm64': '885332ad4aeea3c4b368768d8abfd458e0e48577fb18011755794160777bb7f0',
}


def checked(path, digest):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    return json.loads(path.read_text())


def observations(directory, arch):
    runtime = checked(directory / 'out/runtime.json', RUNTIMES[arch])
    assert runtime['exit'] == 0 and runtime['guest_markers'] == 1 and runtime['started']
    assert len(runtime['prewarmers']) == 1 and runtime['prewarmers'][0]['same_file']
    proof = {}
    if arch == 'amd64':
        prior = checked(directory / 'out/layout.json', '05cacf144a508ac5e89707a90f21c7975f0f90952f60422cdb1750e53d535718')
        assert prior['child'] == ARCHIVES[arch][1] and prior['source'] == SOURCE and prior['host'] == runtime['host']
        for name, row in prior['files'].items():
            # That pinned runner checked ELF64 EM_X86_64, regular file type and
            # every execute bit before saving the SHA, length and lstat mode.
            assert runtime['matches_layout'][name]
            proof[name] = {k: row[k] for k in ('sha256', 'bytes', 'mode')}
            proof[name].update(machine=62, regular=True, executable=True, mode_source='original lstat')
    else:
        original = json.loads((directory.parent / 'arm-original.json').read_text())
        assert not original['errors'] and original['child'] == ARCHIVES[arch][1] and original['original_archive'] == ARCHIVES[arch][0]
        assert not runtime['matches_layout']  # The old reader stopped before recording a local stat.
        for name, row in original['files'].items():
            elf, output = row['elf'], row['output']
            assert elf['machine'] == 'AArch64' and output['path'].endswith('/bin/release/' + name)
            mode = output['unix_mode']
            proof[name] = dict(sha256=elf['sha256'], bytes=elf['bytes'], machine=183, regular=True, executable=output['is_executable'],
                               mode=oct(int(mode) & 0o7777) if mode is not None else None, mode_source='original REv2 OutputFile')
    return {'source': SOURCE, 'host': runtime['host'], 'child': ARCHIVES[arch][1], 'files': proof}


def main():
    root = Path(os.environ['RUNNER_TEMP'])
    for arch, (digest, _) in ARCHIVES.items():
        directory = root / arch
        archive = directory / 'qualification.tar.gz'
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
        inv = directory / 'invocation'
        inv.mkdir()
        with tarfile.open(archive) as tar:
            for name in ('events.jsonl', 'execution.binpb', 'final-head.txt', 'preflight-exit.txt', 'native-exit.txt', 'capture-exit.txt', 'arguments.txt'):
                [item] = [m for m in tar.getmembers() if m.isfile() and m.name.startswith('invocation-') and m.name.endswith('/' + name)]
                (inv / name).write_bytes(tar.extractfile(item).read())
        proof = directory / 'original-observation.json'
        proof.write_text(json.dumps(observations(directory, arch)))
        subprocess.run([sys.executable, str(Path(__file__).with_name('read.py')), str(inv), str(root / 'bb'), str(root / (arch + '-replay.json')), str(proof)],
                       check=True, env={**os.environ, 'QUALIFICATION_ARCH': arch, 'GITHUB_SHA': SOURCE})


if __name__ == '__main__':
    main()
