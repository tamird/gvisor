"""Join the release owner to its declared copy inputs and installed binaries."""

import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qualification_report as reporter


def main():
    inv, decoder, output = map(Path, sys.argv[1:4])
    # Original-only replay supplies the observations from the retained
    # runner/ActionResult instead of re-materializing or running the release.
    replay = json.loads(Path(sys.argv[4]).read_text()) if len(sys.argv) == 5 else None
    events = inv / 'events.jsonl'
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    targets = [reporter.Owner.parse(row['id']['targetCompleted']) for row in rows
               if row.get('id', {}).get('targetCompleted', {}).get('label') == '//:release'
               and not row['id']['targetCompleted'].get('aspect') and row['completed']['success']]
    assert len(targets) == 1
    outputs = reporter.advertised_build_outputs(events, {(targets[0], 'default')})
    names = {'runsc', 'containerd-shim-runsc-v1', *('gvisor-bin/' + name for name in (
        'checkpointgofer', 'gvisor_sentry', 'gvisor-proc-umounter', 'gvisor-sentry-prewarmer',
        'runsc-fd-parking', 'runsc-metric-server'))}
    by_path = {out['name']: out for out in outputs}
    assert len(outputs) == len(by_path) == 8
    source = (inv / 'final-head.txt').read_text().strip()
    assert source == os.environ['GITHUB_SHA']
    assert all((inv / f'{part}-exit.txt').read_text().strip() == '0' for part in ('preflight', 'native', 'capture'))
    proc = subprocess.Popen([str(decoder), 'print', '--raw=true', '--sort=false',
                             '--compact_execution_log=' + str((inv / 'execution.binpb').resolve())], stdout=subprocess.PIPE)
    records, actions, chunk = {}, [], []
    for line in proc.stdout:
        chunk.append(line)
        if line.rstrip(b'\r\n') != b'}':
            continue
        event = json.loads(b''.join(chunk))
        chunk.clear()
        if 'id' in event and ('file' in event or 'inputSet' in event):
            records[event['id']] = event
        if (spawn := event.get('spawn')) and spawn.get('targetLabel') == '//:release' and spawn['mnemonic'] == 'CopyFile':
            actions.append(spawn)
    assert not chunk and proc.wait() == 0 and len(actions) == 8
    source_ids = {}
    for key, event in records.items():
        if file := event.get('file'):
            source_ids.setdefault(file['path'], []).append(key)
    cpu = os.environ['QUALIFICATION_ARCH']
    machine = {'amd64': ('x86_64', 62), 'arm64': ('aarch64', 183)}[cpu]
    child = next(row['started']['uuid'] for row in rows if 'started' in row)
    host = platform.machine()
    if replay is not None:
        assert (replay['source'], replay['child']) == (source, child) and set(replay['files']) == names
        host = replay['host']
    assert host == machine[0]
    # The build lane uses --config=rbe even for --config=aarch64. The copy
    # tool runs on the execution platform; the staged file targets the guest.
    configs = set((inv / 'arguments.txt').read_text().splitlines())
    [worker] = [arch for flag, arch in (('--config=rbe', 'amd64'), ('--config=rbe-arm64', 'arm64')) if flag in configs]
    actual = {}
    for spawn in actions:
        props = {p['name']: p['value'] for p in spawn['platform']['properties']}
        assert props['Arch'] == worker and props['OSFamily'] == 'linux' and props['workload-isolation-type'] == 'oci'
        assert props['container-image'] == (
            'docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef')
        assert spawn['runner'] == 'remote' and not spawn.get('cacheHit') and not spawn.get('exitCode')
        [out] = spawn['outputs']
        file = records[out['outputId']]['file']
        path, digest = file['path'], file['digest']
        tool, command, src, dest = spawn['args']
        assert tool.endswith('/coreutils') and command == 'cp' and dest == path
        source_id = next(key for key in source_ids[src]
                         if reporter.input_chain(records, int(spawn['inputSetId']), key) is not None)
        assert records[source_id]['file']['digest'] == digest
        assert by_path[path]['sha256'] == digest['hash'] and by_path[path]['bytes'] == int(digest['sizeBytes'])
        name = path.split('/bin/release/', 1)[1]
        assert name in names and name not in actual
        if replay is None:
            staged = Path('bazel-bin/release') / name
            mode = staged.lstat().st_mode
            with staged.open('rb') as f:
                head = f.read(20)
                assert head[:6] == b'\x7fELF\x02\x01'
                f.seek(0)
                proof = {'sha256': hashlib.file_digest(f, 'sha256').hexdigest(), 'bytes': staged.stat().st_size,
                         'machine': struct.unpack_from('<H', head, 18)[0], 'regular': stat.S_ISREG(mode),
                         'executable': bool(mode & 0o111), 'mode': oct(stat.S_IMODE(mode)), 'mode_source': 'lstat'}
        else:
            proof = replay['files'][name]
        assert proof['regular'] and proof['executable'] and proof['machine'] == machine[1]
        if proof['mode'] is not None:
            assert int(proof['mode'], 8) & 0o111 == 0o111
        assert proof['sha256'] == digest['hash'] and proof['bytes'] == int(digest['sizeBytes'])
        actual[name] = {**proof,
                        'source': src, 'copy_action': spawn['digest'], 'worker': props}
    assert set(actual) == names
    output.write_text(json.dumps({'source': source, 'child': child, 'host': machine[0], 'files': actual}, indent=2) + '\n')


if __name__ == '__main__':
    main()
