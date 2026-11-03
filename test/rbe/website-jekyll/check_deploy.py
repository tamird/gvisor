"""Exercise the release Make command against a loopback registry and a Cloud Run recorder."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from urllib.request import Request, urlopen

tmp = Path(os.environ['RUNNER_TEMP'])
out = tmp / 'website-release.json'
source = 'a854488bbff86071add9b20367ac1862701a9636'
result = {'source': source, 'carrier': os.environ['GITHUB_SHA'], 'status': 'INCOMPLETE'}


def record():
    result['verified_at'] = datetime.now(timezone.utc).isoformat()
    out.write_text(json.dumps(result, indent=2) + '\n')


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def inspect_manifest(host, ref):
    manifests = ('application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json')
    indexes = ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json')
    req = Request(f'http://{host}/v2/gvisor/site/manifests/{ref}', headers={'Accept': ', '.join((*manifests, *indexes))})
    with urlopen(req, timeout=10) as response:
        raw = response.read()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        assert response.headers['Docker-Content-Digest'] == digest
        data = json.loads(raw)
        kind = response.headers.get_content_type()
    if ref.startswith('sha256:'):
        assert ref == digest
    if kind in indexes:
        assert not ref.startswith('sha256:')
        selected, = (m['digest'] for m in data['manifests'] if m.get('platform', {}).get('os') == 'linux'
                     and m['platform'].get('architecture') == 'amd64')
        assert re.fullmatch('sha256:[0-9a-f]{64}', selected)
        _, child, config = inspect_manifest(host, selected)
        return digest, child, config
    assert kind in manifests
    return digest, digest, data['config']['digest']


def capture(log, bep, pushes, calls):
    raw = log.read_bytes()
    body = raw.decode(errors='replace')
    info = {'log_sha256': hashlib.sha256(raw).hexdigest(), 'log_bytes': len(raw),
            'failure_kinds': [s for s in ('no such package', 'certificate', '404', '429', 'no matching toolchains',
                              'permission denied', 'no space left', 'connection refused', 'timed out',
                              'error downloading', 'unauthorized', 'error 37') if s in body.lower()],
            'push_attempts': pushes.read_text().splitlines().count('push') if pushes.exists() else 0,
            'builder_attempt': pushes.exists() and 'unexpected builder command' in pushes.read_text(),
            'cloud_calls': calls.read_text().splitlines().count('deploy') if calls.exists() else 0,
            'bep_present': bep.exists(), 'bazel_target_completed': False}
    if bep.exists():
        for line in bep.open():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                info['incomplete_bep'] = True
                break
            if e['id'].get('targetCompleted', {}).get('label') == '//website:website':
                info['bazel_target_completed'] = e.get('completed', {}).get('success', False)
            if 'finished' in e:
                info['bazel_exit'] = e['finished'].get('exitCode', {}).get('code', 0)
            action = e.get('action', {})
            if action and not action.get('success'):
                info['failed_mnemonics'] = sorted(set(info.get('failed_mnemonics', [])) |
                                                  {action.get('type', 'UNKNOWN')})[:12]
    return info


def main():
    assert output('git', 'rev-parse', 'HEAD') == source
    assert os.environ['DOCKER_BUILD'] == 'false'
    bazel_dir = Path(os.environ['BUILDKITE_BAZEL_DIRECTORY'])
    assert shutil.which('bazel') == str(bazel_dir / 'bazel')
    stanza = Path('.buildkite/release.yaml').read_text().split('label: ":ship: Website Deploy"\n', 1)[1].split('  - <<:', 1)[0]
    command = shlex.split(re.search(r'^      - (make .+)$', stanza, re.M)[1])
    assert 'queue: "release"' in stanza and 'arch: "amd64"' in stanza
    assert command == ['make', 'BAZEL_OPTIONS=--config=x86_64', 'website-deploy']
    result.update(phase='registry', command=command, bazel=output('bazel', '--version'))
    record()
    docker = shutil.which('docker')
    cid = os.environ['REGISTRY_CONTAINER']
    host = output(docker, 'port', cid, '5000/tcp')
    assert re.fullmatch(r'127\.0\.0\.1:[0-9]+', host)
    tag = host + '/gvisor/site:release'
    command += ['WEBSITE_IMAGE=' + tag, 'WEBSITE_SERVICE=test-service', 'WEBSITE_REGION=test-region',
                'WEBSITE_PROJECT=test-project', 'WEBSITE_PUBLICATION_TIME=2026-11-03T00:00:00Z']
    probe = tmp / 'website-publication-bin'
    probe.mkdir()
    gcloud = probe / 'gcloud'
    gcloud.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$GCLOUD_CALLS"\n')
    gcloud.chmod(0o755)
    wrapper = probe / 'docker'
    wrapper.write_text('''#!/bin/sh
case "$1" in
  push)
    [ "$#" = 2 ] && [ "$2" = "$LOCAL_TAG" ] || exit 70
    echo push >> "$DOCKER_CALLS"
    [ "$PUSH_MODE" != reject ] || exit 37
    ;;
  exec|run|build|buildx) echo "unexpected builder command" >> "$DOCKER_CALLS"; exit 71 ;;
esac
exec "$DOCKER_REAL" "$@"
''')
    wrapper.chmod(0o755)
    rc = tmp / 'website-release.bazelrc'
    env = os.environ | {'PATH': f'{probe}:' + os.environ['PATH'], 'DOCKER_REAL': docker, 'LOCAL_TAG': tag,
                        'MAKEFLAGS': 'STARTUP_OPTIONS=--bazelrc=' + str(rc)}
    for mode in ('allow', 'reject'):
        result['phase'] = mode
        record()
        calls, pushes = (tmp / f'{mode}.{p}' for p in ('gcloud', 'docker'))
        log, bep = (tmp / f'{mode}.{suffix}' for suffix in ('log', 'bep'))
        rc.write_text('build --jobs=4 --remote_cache= --remote_executor= --disk_cache=\n'
                      f'run --build_event_json_file={bep}\n')
        with log.open('wb') as f:
            status = subprocess.run(command, env=env | {'PUSH_MODE': mode, 'DOCKER_CALLS': str(pushes),
                                    'GCLOUD_CALLS': str(calls)}, stdout=f, stderr=subprocess.STDOUT).returncode
        arm = result[mode] = {'make_exit': status, 'make_finished': datetime.now(timezone.utc).isoformat()}
        record()
        arm.update(capture(log, bep, pushes, calls))
        arm['boundary'] = ('builder-called' if arm['builder_attempt'] else
                           'before-push' if not arm['push_attempts'] else
                           'after-push' if not arm['cloud_calls'] else 'cloud-recorder')
        record()
        assert arm['push_attempts'] == 1 and not arm['builder_attempt']
        assert arm.get('bazel_exit') == 0 and arm['bazel_target_completed']
        if mode == 'reject':
            assert status == 2 and not calls.exists() and 'error 37' in arm['failure_kinds']
            result[mode].update(docker_push_exit=37, cloud_calls=0)
            continue
        assert status == 0
        exp = ['run', 'deploy', 'test-service', '--platform=managed', '--region=test-region',
               '--project=test-project', '--image=' + tag, '--memory', '1Gi']
        assert calls.read_text().splitlines() == exp
        image = json.loads(output(docker, 'image', 'inspect', tag))[0]
        assert (image['Os'], image['Architecture'], image['Config']['Entrypoint']) == ('linux', 'amd64', ['/server'])
        top, selected, config = inspect_manifest(host, 'release')
        assert config == image['Id']
        arm.update(image_id=config, manifest_digest=top, amd64_manifest=selected, cloud_calls=1,
                   image_platform='linux/amd64')
        record()
    result['phase'] = 'shutdown'
    record()
    pid = int(output('bazel', '--bazelrc=' + str(rc), 'info', 'server_pid'))
    marker = 'if [[ -n "${BUILDKITE_BAZEL_DIRECTORY:-}" ]]; then'
    hook = Path('.buildkite/hooks/post-command').read_text()
    assert hook.count(marker) == 1 and Path(f'/proc/{pid}').exists()
    shutdown = subprocess.run(['bash', '-c', marker + hook.split(marker)[1]],
                              env=os.environ | {'BUILDKITE_COMMAND_EXIT_STATUS': '0'}).returncode
    try:
        stopped = Path(f'/proc/{pid}/stat').read_text().split(') ')[1][0] == 'Z'
    except FileNotFoundError:
        stopped = True
    result['shutdown'] = {'native_exit': shutdown, 'process_stopped': stopped, 'launcher_removed': not bazel_dir.exists()}
    assert shutdown == 0 and stopped and not bazel_dir.exists()
    result.update(status='PASS', phase='complete', published_to_cloud=False)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        result.update(status='FAIL', error_type=type(exc).__name__)
        raise
    finally:
        record()
