"""Read the original eight ARM64 output files and their copy action results."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import struct
import sys


def request(secret, path, args, limit, consume):
    conn = http.client.HTTPSConnection('app.buildbuddy.io', timeout=90)
    try:
        conn.request('POST', path, json.dumps(args), {'Content-Type': 'application/json', 'x-buildbuddy-api-key': secret})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f'HTTP {response.status}')
        if consume is None:
            data = response.read(limit + 1)
            assert len(data) <= limit
            return json.loads(data)
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            assert size <= limit
            consume(chunk)
        return size
    finally:
        conn.close()


def read_elf(secret, row):
    digest = hashlib.sha256()
    header = bytearray()

    def consume(chunk):
        digest.update(chunk)
        header.extend(chunk[:max(0, 20 - len(header))])

    uri = f"bytestream://remote.buildbuddy.io/blobs/{row['sha256']}/{row['bytes']}"
    size = request(secret, '/api/v1/GetFile', {'uri': uri}, row['bytes'], consume)
    assert size == row['bytes'] and digest.hexdigest() == row['sha256']
    assert header[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', header, 18) == (183,)
    return {'bytes': size, 'sha256': digest.hexdigest(), 'machine': 'AArch64'}


def main():
    source = json.loads(Path(__file__).with_name('arm-original.json').read_text())
    assert source['child'] == '1ca9c3ce-f1ef-4342-9f0d-439307a755e2' and len(source['files']) == 8
    key = os.environ.pop('BUILDBUDDY_API_KEY')
    result = {'child': source['child'], 'original_archive': source['archive'], 'files': {}, 'errors': []}
    by_action = {}
    try:
        result_rows = request(key, '/rpc/BuildBuddyService/GetExecution', {
            'executionLookup': {'invocationId': source['child'], 'targetLabel': '//:release'},
            'inlineExecuteResponse': True,
        }, 8 * 1024 * 1024, None)['execution']
        for row in result_rows:
            if row.get('actionMnemonic') == 'CopyFile':
                by_action.setdefault(row['actionDigest']['hash'], []).append(row)
    except (OSError, http.client.HTTPException, AssertionError, ValueError, KeyError) as exc:
        result['errors'].append(['copy metadata', type(exc).__name__, str(exc)[:64] if isinstance(exc, ValueError) else None])
    for name, row in source['files'].items():
        entry = result['files'][name] = {}
        try:
            entry['elf'] = read_elf(key, row)
        except (OSError, http.client.HTTPException, AssertionError, ValueError) as exc:
            result['errors'].append([name, 'ELF', type(exc).__name__])
        try:
            [execution] = by_action[row['action']['hash']]
            assert execution['targetLabel'] == '//:release' and execution['actionDigest'] == row['action']
            response = execution['executeResponse']
            assert not response.get('cachedResult') and not response.get('status', {}).get('code')
            native = response['result']
            assert not native.get('exitCode')
            [file] = native['outputFiles']
            assert file['path'] == row['path'] and file['digest']['hash'] == row['sha256'] and int(file['digest']['sizeBytes']) == row['bytes']
            entry['output'] = {'path': file['path'], 'is_executable': file.get('isExecutable', False),
                               'unix_mode': file.get('nodeProperties', {}).get('unixMode'), 'action': row['action']['hash']}
            assert entry['output']['is_executable']
        except (OSError, http.client.HTTPException, AssertionError, ValueError, KeyError) as exc:
            result['errors'].append([name, 'output', type(exc).__name__, str(exc)[:64] if isinstance(exc, KeyError) else None])
    Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + '\n')
    assert not result['errors'], result['errors']


if __name__ == '__main__':
    main()
