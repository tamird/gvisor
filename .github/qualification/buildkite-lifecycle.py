"""Reconcile the two native boot benchmarks using the unchanged Make entry."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import zipfile

from qualification_report import benchmark_rows

TMP = Path(os.environ['RUNNER_TEMP']) / 'lifecycle'
OUT = TMP / 'result.json'
BASE = 'https://buildkite.com/organizations/gvisor/pipelines/pipeline/builds/53366/jobs/'
# SHA256 of each full, untruncated Buildkite-rendered output.
JOBS = {
    'oci_lifecycle': ('01a1a149-40ca-4018-ad1b-1c8a269b084c', 'bec511a9f1a0d326b4f1f1b3e7fb507a92b6f81bc77cfc7722ed1adab0ea4c1d', 'BenchmarkRunscDo', 'runsc/container:lifecycle_benchmark_test'),
    'boot_lifecycle': ('01a1a149-40cb-47ce-bf2b-49259a707ee4', '7187b643b416642b835276a014392abf89fa1cd3830b6c467e300241e04408f9', 'BenchmarkSandboxBoot/.*/Boot', 'runsc/container:boot_benchmark_test'),
    'shim_lifecycle': ('01a1a149-40cc-4b12-8ad9-b531d2386fa5', 'ce7476091f2260412942317a0579f8bb001de96bd19510e75571ec501e76d605', 'BenchmarkShimStart', 'shim/tests:lifecycle_benchmark_test'),
    'sandboxexec': ('01a1a149-40cd-49be-87e7-b4ea5eee0d59', '9138c5c9260e157a05a36e79e10852b99bf0f5c0d616c6b64bb51bac1a99f6fa', 'BenchmarkSandboxexecExec', 'sandboxexec/sandbox:sandbox_benchmark_test'),
}
COUNT = re.compile(r'^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d Parsed benchmarks: (\d+)$', re.M)
INPUT = re.compile(r'^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d Parsing output: (goos: linux\n.*?)^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d Parsed benchmarks: (\d+)$', re.M | re.S)
CACHE = '--remote_cache=grpcs://remote.buildbuddy.io'
QUOTED = '--build_metadata=NATIVE_LIFECYCLE=Buildkite benchmark'
DOC = json.loads(OUT.read_text())


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def save():
    DOC['updated_utc'] = datetime.now(timezone.utc).isoformat()
    OUT.write_text(json.dumps(DOC, indent=2) + '\n')


def platforms(suite):
    return ('runsc',) if suite == 'sandboxexec' else ('kvm', 'systrap')


def expected(suite, platform):
    if suite == 'sandboxexec':
        return {'BenchmarkSandboxexec' + n for n in ('New', 'Exec', 'Close', 'EndToEnd')}
    if suite == 'boot_lifecycle':
        return {f'BenchmarkSandboxBoot/{platform}{overlay}/{part}' for part in ('Boot','New','Run') for overlay in ('','_overlay')}
    owners = json.loads((TMP/'manifest.json').read_text())['owners']
    pkg = JOBS[suite][3].split(':')[0]
    names = {row['name'] for row in owners[f'//{pkg}:lifecycle_benchmark_{platform}_test']}
    if suite == 'oci_lifecycle':
        names.add(f'BenchmarkRunscDo/{platform}')
    return names


def inspect_inputs(suite, console, prefix, narrow):
    matches = INPUT.findall(console)
    require(len(matches) == len(platforms(suite)), 'missing parser input')
    records = {}
    for platform, (body, count) in zip(platforms(suite), matches):
        path = TMP/f'{prefix}-{suite}-{platform}.log'
        path.write_text(body)
        rows = benchmark_rows(path, pgo=True)
        wanted = expected(suite, platform)
        if narrow:
            if suite == 'boot_lifecycle':
                wanted = {n for n in wanted if n.endswith('/Boot')}
            else:
                wanted = {n for n in wanted if n.split('/')[0] == JOBS[suite][2]}
        require({r['name'] for r in rows} == wanted and len(rows) == int(count) == len(wanted), 'wrong benchmark census')
        require(all(r['iterations'] == 30 and {'ns/op','p50.ns','p90.ns'} <= set(r['metrics']) for r in rows), 'wrong iterations or metric')
        records[platform] = dict(rows=sorted(wanted), iterations=30, parser_count=int(count), input_sha256=sha256(body.encode()).hexdigest())
    return records


def originals():
    archive = TMP/'original.zip'
    digest = sha256(archive.read_bytes()).hexdigest()
    require(digest == '0eeb34c90bf9d72f2fb7c5f7aec8edfec643f5acf2396466d1f73c16656af5bd', 'wrong original')
    with zipfile.ZipFile(archive) as z:
        require(z.namelist() == ['result.json'], 'wrong original archive')
        prior = json.loads(z.read('result.json'))
    require(prior['status'] == 'FAIL' and prior['reason'] == 'native samples incomplete'
            and prior['product'] == os.environ['PRODUCT']
            and prior['integration'] == os.environ['INTEGRATION']
            and prior['compiler'] == os.environ['COMPILER']
            and prior['original_source'] == os.environ['MASTER'], 'wrong prior identities')
    boot = prior['native']['boot_lifecycle']
    require(boot['counts'] == ['0', '0'] and boot['make_exit'] == 2 and boot['input_count'] == 2
            and boot['stopped_at_missing_adc'] == 0
            and boot['console_sha256'] == '3c5e0aa10bee8281f6f93b790c5ac458fcd2fbbba34a4839c21f55ef0fd98f5f', 'wrong original boot failure')
    controls = {}
    for suite, (job, digest0, _, _) in JOBS.items():
        original = prior['originals'][suite]
        require(original['job'] == job and original['output_sha256'] == digest0, 'wrong production source')
        for platform in platforms(suite):
            label = suite + '/' + platform
            names = expected(suite, platform)
            measurement = original['results'][platform]
            replay = prior['replay'][label]
            require(set(measurement['rows']) == names and measurement['iterations'] == 30
                    and measurement['parser_count'] == len(names) and replay['missing_adc']
                    and replay['counts'] == [str(len(names))], 'wrong production rows')
            if suite != 'boot_lifecycle':
                passed = prior['native'][suite]['results'][platform]
                require(passed['parser_count'] == len(passed['rows']) > 0 and passed['iterations'] == 30,
                        'missing retained native sample')
                controls[label] = passed
    require(len(controls) == 5, 'wrong retained sample count')
    DOC.update(product=prior['product'], original_source=prior['original_source'], originals=prior['originals'],
               replay=prior['replay'], integration=prior['integration'], compiler=prior['compiler'],
               original=dict(artifact=13252074858, sha256=digest, status=prior['status'], boot=boot, accepted=controls))
    data = TMP/'diagnosis.zip'
    sha = sha256(data.read_bytes()).hexdigest()
    require(sha == 'd24a62081dcdb6e12dc8c63a8bd37ec9b50b61d90640c1e86f29c05ff73879aa', 'wrong diagnosis archive')
    with zipfile.ZipFile(data) as z:
        require(z.namelist() == ['result.json'], 'wrong diagnosis files')
        diagnosis = json.loads(z.read('result.json'))
    original = diagnosis['native']['boot_lifecycle']
    require(diagnosis['status'] == 'FAIL' and diagnosis['integration'] == os.environ['INTEGRATION']
            and original['counts'] == ['0','0'] and original['make_exit'] == 2, 'wrong original diagnosis')
    for v in original['children'].values():
        require(v['binary_exit'] == v['sudo_exit'] == 1 and any(
            'Copy:' in line and 'copy_file_range: is a directory' in line for line in v['stdout']['failures']),
            'wrong original child')
    require(set(original['children']) == {'kvm','systrap'}, 'missing original child')
    DOC['original_diagnosis'] = dict(artifact=13255956897, sha256=sha, unmodified_integration=diagnosis['integration'],
                                     children=original['children'], make_exit=original['make_exit'])
    DOC.update(lookup_product=os.environ['LOOKUP'], fixed_source=os.environ['FIXED'], fixed_tree=os.environ['FIXED_TREE'])
    DOC['stage'] = 'native'
    save()


def lookup():
    owner = '//pkg/test/testutil:testutil_test'
    bep = TMP/'lookup.bep'
    log = Path('bazel-testlogs/pkg/test/testutil/testutil_test/test.log')
    with (TMP/'lookup.console').open('w') as console:
        rc = subprocess.run(['bazel','test', CACHE, '--nocache_test_results','--test_filter=^TestRunfileExecutable$',
                             '--test_arg=-test.v','--build_event_json_file='+str(bep),owner],
                            stdout=console, stderr=subprocess.STDOUT).returncode
    row = DOC['lookup_cases'] = dict(owner=owner, bazel_exit=rc)
    save()
    results = []
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        if (event['id'].get('testResult') or {}).get('label') == owner:
            r = event['testResult']
            results.append(dict(status=r['status'], cached=bool(r.get('cachedLocally') or r.get('cachedRemotely') or (r.get('executionInfo') or {}).get('cachedRemotely'))))
    row['results'] = results
    if log.is_file():
        row['cases'] = re.findall(r'^\s*--- (PASS|FAIL|SKIP): (TestRunfileExecutable(?:/[^\s]+)?) \(', log.read_text().replace(chr(22), ""), re.M)
        row['log_sha256'] = sha256(log.read_bytes()).hexdigest()
    save()
    names = ['TestRunfileExecutable'] + ['TestRunfileExecutable/'+n for n in ('direct','rules_go','wrapped','not_executable')]
    require(rc == 0 and results == [dict(status='PASSED',cached=False)]
            and sorted(row.get('cases',[])) == sorted(('PASS',name) for name in names), 'runfile fixture did not execute')


def error_lines(log):
    # Only compiler / Make diagnostic lines from an anonymous workspace. Keep
    # URLs, flags, environment assignments and quoted command lines private.
    lines = []
    for text in log.splitlines():
        if len(text) > 2400 or re.search(r'(?i)token|credential|authoriz|secret|signature|api.key|password|--|/bin/(?:ba)?sh -c',text):
            continue
        if not re.search(r'(?i)^error:|error |error:|fatal:|failed to |no such file|not found|permission denied|cannot |command not found|make: \*\*\*|exec format|operation not permitted|^cp:|^xargs:', text):
            continue
        text = re.sub(r'(?i)(?:[a-z][a-z0-9+.-]*://|/home/runner/_work/_actions/)[^\s]+','[location]', text)
        if len(text) <= 510 and text not in lines:
            lines.append(text)
        if len(lines) == 14:
            break
    return lines


def boot_child(args):
    # The selected Make entry calls sudo -E once per platform; preserve all
    # other sudo calls exactly. Keep the child's status before GNU xargs maps it.
    if len(args) < 2 or args[0] != '-E' or Path(args[1]).name != 'boot_benchmark_test':
        os.execv('/usr/bin/sudo', ['sudo', *args])
    exe = Path(args[1])
    platform, = [a.removeprefix('--test_platforms=') for a in args if a.startswith('--test_platforms=')]
    require(platform in platforms('boot_lifecycle') and exe.is_absolute()
            and args[2:] == ['--test_platforms=' + platform, '-test.v',
                             '-test.bench=' + JOBS['boot_lifecycle'][2], '-test.benchtime=30x'], 'unexpected boot arguments')
    stdout, stderr, status = (TMP / (platform + suffix) for suffix in ('.stdout', '.stderr', '.exit'))
    row = dict(executable_sha256=sha256(exe.read_bytes()).hexdigest(), platform=platform, args=args[2:])
    def retain():
        (TMP / ('child-' + platform + '.json')).write_text(json.dumps(row))
    pkg = Path(str(exe)+'.runfiles')/'_main/test/cmd/test_app'
    direct = pkg/'test_app'
    nested = direct/'test_app'
    layout = row['runfiles'] = dict(direct_directory=direct.is_dir(), nested_regular=nested.is_file())
    if layout['nested_regular']:
        with nested.open('rb') as f:
            layout.update(elf_magic=f.read(4).hex() == '7f454c46', executable=os.access(nested,os.X_OK))
        layout['sha256'] = sha256(nested.read_bytes()).hexdigest()
    retain()
    with stdout.open('xb') as out, stderr.open('xb') as err:
        # Only run the already selected benchmark under sudo. The shell stores
        # its direct exit before sudo, two xargs instances and Make return.
        rc = subprocess.run(['/usr/bin/sudo', '-E', '/bin/sh', '-c',
                              '"$@"; rc=$?; printf "%s\n" "$rc" > "$0"; exit "$rc"',
                              str(status), *args[1:]], stdin=sys.stdin, stdout=out, stderr=err).returncode
    row.update(sudo_exit=rc, binary_exit=int(status.read_text()) if status.exists() else None)
    retain()
    for stream, path, dest in (('stdout', stdout, sys.stdout.buffer), ('stderr', stderr, sys.stderr.buffer)):
        data = path.read_bytes()
        lines = data.decode(errors='replace').splitlines()
        report = row[stream] = dict(bytes=len(data), sha256=sha256(data).hexdigest(),
            milestones=[l for l in lines if l in ('goos: linux','goarch: amd64','FAIL','PASS') or
                        re.fullmatch(r'BenchmarkSandboxBoot(?:/(?:systrap|kvm)(?:_overlay)?(?:/(?:Boot|Run|New))?)?', l)],
            failures=[])
        for line in lines:
            # Keep only a source-located Go test diagnostic or a named failure,
            # never the full command, environment, or setup trace.
            if not re.match(r'^\s*(?:[\w./-]+\.go:\d+:|--- FAIL: BenchmarkSandboxBoot\b|Error running as root:|panic:|sudo:)', line):
                continue
            if re.search(r'(?i)token|credential|password|authorization|api.key|secret|https?://|(?:^|\s)[A-Z_]{3,}=', line):
                continue
            text = re.sub(r'/(?:home/runner|tmp|var/tmp)/[^\s"\x27,:;)]+', '[runner path]', line.strip())
            text = re.sub(r'\b[0-9a-f]{32,}\b', '[id]', text)
            excerpt = text if len(text) <= 560 else text[:250] + ' … ' + text[-300:]
            if excerpt not in report['failures'] and len(report['failures']) < 18:
                report['failures'].append(excerpt)
        if stream == 'stdout' and row['binary_exit'] == 0:
            try:
                report['measurements'] = [r for r in benchmark_rows(path, pgo=True) if r['name'] in expected('boot_lifecycle',platform)]
            except (ValueError, OSError) as exc:
                report['parse_error'] = type(exc).__name__
        retain()
        dest.write(data)
        dest.flush()
    return rc


def native():
    import yaml
    pipe = yaml.safe_load(Path('.buildkite/pipeline.yaml').read_text())
    selected = [s for s in pipe['steps'] if isinstance(s, dict) and s.get('command','').startswith(('make benchmark-nodocker ', 'make benchmark-platforms-nodocker '))]
    require(len(selected) == 4, 'wrong Buildkite selection')
    selected = [s for s in selected if 'BENCHMARKS_SUITE=boot_lifecycle' in shlex.split(s['command'])]
    require(len(selected) == 1, 'wrong boot selection')
    directory = os.environ['BUILDKITE_BAZEL_DIRECTORY']
    actual = Path(directory)/'bazel'
    require(Path(shutil.which('bazel')) == actual, 'native hook Bazel not first')
    require(subprocess.check_output([actual,'--version'],text=True).strip() == 'bazel '+Path('.bazelversion').read_text().strip(), 'wrong Bazel version')
    guards = TMP/'guards'
    guards.mkdir()
    def wrapper(name, script):
        target = guards/name
        target.write_text(script)
        target.chmod(0o755)
    # This wrapper records the actual build/query labels, then execs exactly
    # the auto-provisioned launcher from pre-command. No secrets in this job.
    wrapper('bazel', """#!/usr/bin/python3
import json, os, subprocess, sys
from pathlib import Path
args=sys.argv[1:]
p=os.environ['BUILDKITE_BAZEL_DIRECTORY']+'/bazel'
result=subprocess.run([p,*args])
with (Path(os.environ['RUNNER_TEMP'])/'lifecycle'/'bazel-calls').open('a') as f:
    f.write(json.dumps([args[0], args[-1], args.count('--remote_cache=grpcs://remote.buildbuddy.io'), args.count('--build_metadata=NATIVE_LIFECYCLE=Buildkite benchmark'), os.getuid(), result.returncode])+'\\n')
raise SystemExit(result.returncode)
""")
    wrapper('docker', '#!/bin/sh\ntouch "$RUNNER_TEMP/lifecycle/docker.called"\nexit 125\n')
    wrapper('sudo', '#!/bin/sh\nexec /usr/bin/python3 -B "$RUNNER_TEMP/lifecycle/buildkite-lifecycle.py" child "$@"\n')
    env = dict(os.environ, PATH=str(guards)+':'+os.environ['PATH'],
               GOOGLE_APPLICATION_CREDENTIALS=str(TMP/'deliberately-absent-adc.json'),
               GCE_METADATA_HOST='127.0.0.1:1', BAZEL_REMOTE_CACHE=CACHE, BAZEL_OPTIONS="'"+QUOTED+"'")
    require(not Path(env['GOOGLE_APPLICATION_CREDENTIALS']).exists(), 'unexpected Google credentials')
    DOC.update(native={}, docker_calls=None, probes={n: bool(shutil.which(n)) for n in ('gcc','aarch64-linux-gnu-gcc','qemu-aarch64','objcopy','ld')})
    require(not DOC['probes']['aarch64-linux-gnu-gcc'] and not Path('/usr/bin/aarch64-linux-gnu-gcc').exists(), 'cross gcc preinstalled')
    save()
    for step in selected:
        args=shlex.split(step['command'])
        opts=dict(arg.split('=',1) for arg in args[2:])
        suite=opts['BENCHMARKS_SUITE']
        require(suite in JOBS and opts['BENCHMARKS_TARGETS'] == JOBS[suite][3] and opts['BENCHMARKS_OPTIONS'] == '-test.benchtime=30x', 'wrong Make command')
        configured={k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in step['env'].items()}
        require(configured['DOCKER_BUILD'] == 'false' and configured['BUILDKITE_PIPELINE_INSTALL_RUNTIME'] == 'false' and configured['BENCHMARKS_UPLOAD'] == 'true' and configured['BENCHMARKS_PLATFORMS'] == 'kvm systrap', 'wrong Make environment')
        # Keep the actual parser path. ADC discovery must fail before creating
        # a client; supply a non-production project as a second independent bound.
        args += ['BENCHMARKS_FILTER='+JOBS[suite][2], 'BENCHMARKS_PROJECT=local-qualification-only']
        callfile=TMP/'bazel-calls'
        previous=len(callfile.read_text().splitlines()) if callfile.exists() else 0
        with (TMP/f'native-{suite}.console').open('wb') as log:
            rc=subprocess.run(args,env=env|configured,stdout=log,stderr=subprocess.STDOUT).returncode
        log=(TMP/f'native-{suite}.console').read_text(errors='replace')
        row=DOC['native'][suite] = dict(make_exit=rc, input_count=len(INPUT.findall(log)), counts=COUNT.findall(log),
          stopped_at_missing_adc=log.count('failed to initialize client on project: local-qualification-only:'),
          diagnostics=[c for c,s in {'no_target':'no such target','compile':'compilepkg:', 'kvm':'failed to create KVM','permission':'permission denied','fetch':'Error downloading','stdlib':'GoStdlib','sandbox':'cannot create gofer','missing_file':'no such file'}.items() if s.lower() in log.lower()])
        calls=[json.loads(line) for line in callfile.read_text().splitlines()[previous:]] if callfile.exists() else []
        row['bazel_calls'] = calls
        row['children'] = {}
        for platform in platforms(suite):
            childfile = TMP / ('child-' + platform + '.json')
            if childfile.exists():
                row['children'][platform] = json.loads(childfile.read_text())
        row['errors'] = error_lines(log)
        row['console_sha256'] = sha256(log.encode()).hexdigest()
        row['docker_called'] = (TMP/'docker.called').exists()
        save()
        try:
            require(rc == 2 and row['stopped_at_missing_adc'] == len(platforms(suite))
                    and env['GOOGLE_APPLICATION_CREDENTIALS'] in log, 'wrong reporting boundary')
            require(set(row['children']) == set(platforms(suite)) and
                    all(c['binary_exit'] == c['sudo_exit'] == 0 for c in row['children'].values()), 'native child failed')
            for name, child in row['children'].items():
                layout = child['runfiles']
                require(layout['direct_directory'] and layout['nested_regular'] and layout['elf_magic'] and layout['executable'],
                        'missing real wrapped ELF')
                rows = child['stdout'].get('measurements', [])
                require(len(rows) == 2 and {r['name'] for r in rows} == {
                    n for n in expected(suite,name) if n.endswith('/Boot')} and all(
                    r['iterations'] == 30 and {'ns/op','p50.ns','p90.ns'} <= set(r['metrics']) for r in rows),
                        'wrong child samples')
            row['results']=inspect_inputs(suite,log,'fresh',True)
        except ValueError as e:
            row['error']=str(e)
        save()
    calls=[json.loads(line) for line in (TMP/'bazel-calls').read_text().splitlines()]
    targets=[row[1] for row in calls if row[0]=='build']
    normalized=lambda s:s.lstrip('/')
    DOC['observed_build_targets']=targets
    save()
    require({normalized(target) for target in targets} == {JOBS['boot_lifecycle'][3], 'tools/parsers:parser', ':release'}, 'native producer missing')
    require(all(a[0] in ('cquery','build') and a[2:4]==[1,1] and a[4]==os.getuid() and a[5]==0 for a in calls), 'wrong Make Bazel options/user')
    DOC['native_bazel']=dict(builds=targets, queries=sum(c[0]=='cquery' for c in calls),all_cache_and_quoted_options=True,caller_uid=os.getuid(),hook_command=str(actual.name))
    DOC['docker_calls']=int((TMP/'docker.called').exists())
    require(not DOC['docker_calls'] and set(DOC['native']) == {'boot_lifecycle'}, 'unexpected builder or suite')
    require(all('results' in v for v in DOC['native'].values()), 'native samples incomplete')
    require(all(v['native_exit']==1 and v['missing_adc'] and v['counts']==[str(len(expected(*k.split('/'))))] for k,v in DOC['replay'].items()), 'original replay incomplete')
    DOC['status']='PASS'
    save()


if __name__=='__main__':
    if sys.argv[1] == 'child':
        raise SystemExit(boot_child(sys.argv[2:]))
    try:
        if sys.argv[1]=='originals':
            originals()
        elif sys.argv[1]=='lookup':
            lookup()
        else:
            native()
    except Exception as error:
        DOC.update(status='FAIL',error=type(error).__name__)
        if isinstance(error, ValueError) and str(error) in {'wrong Make Bazel options/user','native producer missing','native samples incomplete','original replay incomplete','wrong original log','wrong benchmark census','wrong iterations or metric','missing parser input'}:
            DOC['reason']=str(error)
        save()
        raise SystemExit(1) from error
