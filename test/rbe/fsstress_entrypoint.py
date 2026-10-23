"""Prove the unchanged image entrypoint reaches both filesystem workers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

os.chdir(os.environ['BUILD_WORKSPACE_DIRECTORY'])
destination = Path(sys.argv[1])
expected_image = sys.argv[2]
record = {'status': 'INCOMPLETE', 'expected_image': expected_image, 'commands': []}


def run(arguments):
    remaining = float(os.environ['APPLICABILITY_WORK_DEADLINE']) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Entrypoint smoke exhausted the existing work deadline')
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=min(remaining, 120))
    record['commands'].append({'arguments': arguments, 'native_exit': result.returncode,
                               'stdout': result.stdout, 'stderr': result.stderr})
    result.check_returncode()
    return result.stdout


try:
    image = json.loads(run(['docker', 'image', 'inspect', 'gvisor.dev/images/basic/fsstress:latest']))[0]
    record['image'] = image
    if image['Id'] != expected_image or image['Config']['Entrypoint'] != ['/run.sh']:
        raise RuntimeError('Materialized image differs from the qualified image/entrypoint')
    with tempfile.TemporaryDirectory(prefix='fsstress-entrypoint-') as temporary:
        directory = Path(temporary)
        directory.chmod(0o777)
        if any(directory.iterdir()):
            raise RuntimeError('Filesystem smoke directory is not empty')
        run(['docker', 'run', '--rm', '--runtime=runc',
             '--mount=type=bind,src=' + str(directory) + ',dst=/test',
             expected_image, '-d', '/test', '-n', '4', '-p', '2', '-s', '1',
             '-z', '-f', 'creat=1', '-c', '-X', '-v'])
        record['created_files'] = [
            {'path': str(path.relative_to(directory)), 'bytes': path.stat().st_size,
             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(directory.rglob('*')) if path.is_file()]
        if len(record['created_files']) != 8 or any(
                sum(Path(item['path']).parts[0] == worker for item in record['created_files']) != 4
                for worker in ('p0', 'p1')):
            raise RuntimeError('Both fsstress workers did not perform their four create operations')
        record['status'] = 'PASS'
finally:
    destination.write_text(json.dumps(record, indent=2) + '\n')
