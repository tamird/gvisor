"""Exercise the rendered Buildkite syscall command on a native Linux worker."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse

STAGE = Path(os.environ['RUNNER_TEMP']) / 'source-syscalls'
PRODUCT = 'e46a101fc0aebb325ec25977697a012a2e11a11b'
BASE = 'f51a205a8f4afbc5ab41aded070df74951b081ea'
ARCH = 'ee4401662324ea22a54682fef2513d3612a5db2c'
PARENT = '48f6def325100b17fd6a57894b6f7d1c9630805d'
EVENTFD = '//test/syscalls:eventfd_test_runsc_systrap_shared'
PIPE = '//test/syscalls:pipe_external_test_runsc_systrap_directfs'
OUT = STAGE / 'result.json'
RESULT = {'product': PRODUCT, 'base': BASE, 'architecture_selection': PARENT, 'steps': [], 'tests': []}


def write():
    OUT.write_text(json.dumps(RESULT, indent=2) + '\n')


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def pipeline(sha, path):
    source = subprocess.check_output(['git', 'show', f'{sha}:{path}'])
    # The agent requires a nonempty token even for its offline dry-run.
    env = {**os.environ, 'BUILDKITE_AGENT_ACCESS_TOKEN': 'offline-dry-run'}
    data = subprocess.check_output([STAGE/'buildkite-agent', 'pipeline', 'upload', '--dry-run', '--format', 'json'], input=source, env=env)
    return json.loads(data)['steps']


def check_steps():
    selected = None
    for path in ('.buildkite/pipeline.yaml', '.buildkite/first-pass.yaml'):
        before, after = [pipeline(source, path) for source in (BASE, PRODUCT)]
        assert len(before) == len(after)
        for old, new in zip(before, after):
            cmd = old.get('command', '') if isinstance(old, dict) else ''
            if not isinstance(cmd, str) or not cmd.endswith(' syscall-tests'):
                assert new == old
                continue
            key_value = cmd.split()[1].split('=', 1)
            entry = {'path': path, 'label': new['label'], 'selection': key_value[1], 'parallelism': new['parallelism']}
            if 'if' not in old:
                assert new == old
                entry['staged'] = True
            else:
                assert 'build.env("STAGED_BINARIES") == null' in old['if']
                assert {k: v for k, v in new.items() if k not in ('command', 'env')} == {k: v for k, v in old.items() if k != 'command'}
                assert new['env'] == {'DOCKER_BUILD': 'false', 'SYSCALL_TEST_OPTION': key_value[1]}
                assert new['command'].startswith('bazel test --config=buildkite-syscalls ') and '$(uname -r)' in new['command']
                entry['staged'] = False
                if key_value[1] == '--config=syscalls-amd64':
                    selected = new
            RESULT['steps'].append(entry)
    assert sum(r['staged'] for r in RESULT['steps']) == 2 and len(RESULT['steps']) == 8 and selected
    write()
    return selected


def stderr_evidence(log):
    text = log.read_text(errors='replace')
    patterns = {'missing_cross_compiler': r'(aarch64-linux-gnu-(gcc|g\+\+).*?(not found|No such file)|No such file.*aarch64-linux-gnu-(gcc|g\+\+))',
                'cross_link': r'(/bin/ld:.*(aarch64|183)|file in wrong format)', 'compiler': r'(fatal error|error):',
                'sandbox_permission': r'Operation not permitted', 'disk_full': r'No space left on device',
                'missing_target': r'no such target', 'timeout': r'Test timed out|TIMEOUT'}
    classes = [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]
    lines = [line[:360] for line in text.splitlines() if re.search(r'^ERROR:|fatal error:|aarch64-linux-gnu.*not found|Operation not permitted|file in wrong format', line)]
    for line in lines[:8]:
        print(line, flush=True)
    return classes


def phase(step, sha, part):
    assert sha in (PRODUCT, ARCH) and git('rev-parse', 'HEAD') == sha
    label = f'{sha[:9]}-{part}'
    phase_dir = STAGE / label
    phase_dir.mkdir()
    rc = phase_dir / 'test.bazelrc'
    rc.write_text('build --jobs=4\ntest --nocache_test_results\ntest --strategy=TestRunner=linux-sandbox\ntest --test_timeout=300\n')
    env = {**os.environ, **step['env'], 'PARTITION': str(part), 'TOTAL_PARTITIONS': '2', 'TARGETS': EVENTFD+' '+PIPE,
           'CHECK_RC': str(rc), 'RUNTIME': 'buildkite_runtime'}
    if part == 2: env['HOST_KERNEL'] = 'source-syscall-kernel-control'
    actual_kernel = env.get('HOST_KERNEL', os.uname().release)
    cmd = 'bazel() { command bazel --nosystem_rc --nohome_rc --bazelrc="$CHECK_RC" "$@"; }\n' + step['command']
    log = phase_dir / 'bazel.log'
    bep = Path('.build_events.json')
    if bep.exists(): bep.unlink()
    row = {'source': sha, 'partition': part, 'rows': []}
    RESULT['tests'].append(row)
    write()
    with log.open('wb') as stream:
        proc = subprocess.run(['timeout','--kill-after=20','3000','bash','-euc',cmd], env=env, stdout=stream, stderr=subprocess.STDOUT)
    row['exit'] = proc.returncode
    row['diagnostics'] = stderr_evidence(log) if proc.returncode else []
    opts = []
    if bep.is_file():
        row['bep_sha256'] = hashlib.sha256(bep.read_bytes()).hexdigest()
        with bep.open() as stream:
            for line in stream:
                event = json.loads(line)
                if event.get('structuredCommandLine', {}).get('commandLineLabel') == 'canonical':
                    opts = [opt.get('combinedForm','') for section in event['structuredCommandLine']['sections'] for opt in section.get('optionList',{}).get('option',[])]
                tr = event.get('testResult'); owner=event.get('id',{}).get('testResult',{}).get('label')
                if tr is None or owner not in (EVENTFD,PIPE): continue
                info=tr.get('executionInfo',{}); run={'owner':owner,'status':tr['status'],'runner':info.get('strategy'), 'cached':tr.get('cachedLocally',False) or info.get('cachedRemotely',False),'cases':[]}
                for f in tr.get('testActionOutput',[]):
                    if f['name'] != 'test.log':continue
                    parsed=urlparse(f['uri']); assert parsed.scheme=='file'
                    content=Path(unquote(parsed.path)).read_bytes();run['log_sha256']=hashlib.sha256(content).hexdigest();
                    if tr['status'] != 'PASSED':
                        lines = content.decode(errors='replace').splitlines()
                        run['failure_lines'] = [line[:300] for line in lines if re.search(r'FAIL|Error|cannot|failed to|not permitted', line, re.I)][:12]
                    run['cases']+=re.findall(r'\[\s*OK\s*\]\s+((?:EventfdTest\.\w+|Paths/HostPipeTest\.(?:Read|Write)/[01]))\b',content.decode(errors='replace'))
                row['rows'].append(run)
    row['kernel_key'] = '--test_env=HOST_KERNEL='+actual_kernel in opts
    row['partition_keys'] = all('--test_env='+name in opts for name in ('PARTITION','TOTAL_PARTITIONS'))
    row['tmp_mount_options'] = [v for v in opts if v.startswith('--sandbox_add_mount_pair=')]
    write()
    return row


def main():
    assert os.uname().machine == 'x86_64' and os.getuid() != 0
    step=check_steps()
    subprocess.run(['git','switch','--quiet','--detach',PRODUCT],check=True)
    first=phase(step,PRODUCT,1)
    if first['exit'] and not first['rows'] and {'missing_cross_compiler','cross_link'}.intersection(first['diagnostics']):
        subprocess.run(['git','switch','--quiet','--detach',ARCH],check=True)
        first=phase(step,ARCH,1)
    assert first['exit'] == 0,first['diagnostics']
    second=phase(step,first['source'],2)
    assert second['exit'] == 0,second['diagnostics']
    for row in (first,second):
        assert row['kernel_key'] and row['partition_keys'] and not row['tmp_mount_options'],row
        assert {x['owner'] for x in row['rows']}=={EVENTFD,PIPE} and all(x['runner']=='linux-sandbox' and not x['cached'] and x['status']=='PASSED' and x['cases'] for x in row['rows']),row
    for owner in (EVENTFD,PIPE):
        parts = [[c for x in row['rows'] if x['owner']==owner for c in x['cases']] for row in (first,second)]
        if owner==EVENTFD:
            expected={'EventfdTest.'+n for n in re.findall(r'^TEST\(EventfdTest, (\w+)\)',Path('test/syscalls/linux/eventfd.cc').read_text(),re.M)}
        else:
            expected={f'Paths/HostPipeTest.{case}/{instance}' for case in ('Read','Write') for instance in (0,1)}
        assert all(parts) and not set(parts[0])&set(parts[1]) and sorted(parts[0]+parts[1])==sorted(expected),parts
    RESULT['verified']=True


try:
    main()
finally:
    write()
