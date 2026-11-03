"""Qualify the four native Buildkite Make entries against today's original jobs."""
from datetime import datetime, timezone
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from urllib.request import urlopen

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
    require(digest == '6108b2c206d8315d82008b2a839f7c472187eac5a87b18c5ff300d21c136f337', 'wrong original')
    with zipfile.ZipFile(archive) as z:
        prior = json.loads(z.read('result.json'))
    require(prior['product'] == os.environ['PRODUCT'] and prior['original_source'] == os.environ['MASTER']
            and prior['status'] == 'FAIL' and prior['cleanup'] == 'success' and prior['reason'] == 'native producer missing',
            'wrong original result')
    require(not prior['probes']['aarch64-linux-gnu-gcc'] and prior['original']['artifact'] == 13250084690,
            'wrong failed producer environment')
    for native in prior['native'].values():
        require(native['make_exit'] == 2 and not native['docker_called'] and native['input_count'] == 0
                and any('/usr/bin/aarch64-linux-gnu-gcc: No such file or directory' in msg for msg in native['errors']),
                'wrong original producer failure')
    for suite, (job, digest0, _, _) in JOBS.items():
        owner = prior['originals'][suite]
        require(owner['job'] == job and owner['output_sha256'] == digest0, 'wrong original job')
        for platform in platforms(suite):
            names = expected(suite, platform)
            measurement = owner['results'][platform]
            replay = prior['replay'][suite+'/'+platform]
            require(set(measurement['rows']) == names and measurement['parser_count'] == len(names)
                    and measurement['iterations'] == 30 and replay['native_exit'] == 1 and replay['missing_adc']
                    and replay['counts'] == [str(len(names))], 'wrong retained parser result')
    DOC.update(product=prior['product'], original_source=prior['original_source'], originals=prior['originals'],
               replay=prior['replay'], integration=os.environ['INTEGRATION'], compiler=os.environ['COMPILER'], original=dict(artifact=13252397097, sha256=digest, status=prior['status'],
               failed_native=prior['native'], parser_sha256=prior['original']['parser_sha256'], first_artifact=prior['original']['artifact']))
    save()


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


def native():
    import yaml
    pipe = yaml.safe_load(Path('.buildkite/pipeline.yaml').read_text())
    selected = [s for s in pipe['steps'] if isinstance(s, dict) and s.get('command','').startswith(('make benchmark-nodocker ', 'make benchmark-platforms-nodocker '))]
    require(len(selected) == 4, 'wrong Buildkite selection')
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
        row['errors'] = error_lines(log)
        row['console_sha256'] = sha256(log.encode()).hexdigest()
        row['docker_called'] = (TMP/'docker.called').exists()
        save()
        try:
            require(rc == 2 and row['stopped_at_missing_adc'] == len(platforms(suite))
                    and env['GOOGLE_APPLICATION_CREDENTIALS'] in log, 'wrong reporting boundary')
            row['results']=inspect_inputs(suite,log,'fresh',True)
        except ValueError as e:
            row['error']=str(e)
        save()
    calls=[json.loads(line) for line in (TMP/'bazel-calls').read_text().splitlines()]
    targets=[row[1] for row in calls if row[0]=='build']
    normalized=lambda s:s.lstrip('/')
    DOC['observed_build_targets']=targets
    save()
    require({normalized(target) for target in targets} >= {x[3] for x in JOBS.values()} | {'tools/parsers:parser', ':release'}, 'native producer missing')
    require(all(a[0] in ('cquery','build') and a[2:4]==[1,1] and a[4]==os.getuid() and a[5]==0 for a in calls), 'wrong Make Bazel options/user')
    DOC['native_bazel']=dict(builds=targets, queries=sum(c[0]=='cquery' for c in calls),all_cache_and_quoted_options=True,caller_uid=os.getuid(),hook_command=str(actual.name))
    DOC['docker_calls']=int((TMP/'docker.called').exists())
    require(not DOC['docker_calls'] and set(DOC['native']) == set(JOBS), 'unexpected builder or suite')
    require(all('results' in v for v in DOC['native'].values()), 'native samples incomplete')
    require(all(v['native_exit']==1 and v['missing_adc'] and v['counts']==[str(len(expected(*k.split('/'))))] for k,v in DOC['replay'].items()), 'original replay incomplete')
    DOC['status']='PASS'
    save()


if __name__=='__main__':
    try:
        if sys.argv[1]=='originals':
            originals()
        else:
            native()
    except Exception as error:
        DOC.update(status='FAIL',error=type(error).__name__)
        if isinstance(error, ValueError) and str(error) in {'wrong Make Bazel options/user','native producer missing','native samples incomplete','original replay incomplete','wrong original log','wrong benchmark census','wrong iterations or metric','missing parser input'}:
            DOC['reason']=str(error)
        save()
        raise SystemExit(1) from error
