"""Run Buildkite's failure-log collector over native Bazel pass/fail/retry events."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit
repo = Path.cwd()
work = Path(os.environ['RUNNER_TEMP'])/'host-testlogs'
work.mkdir()
proof = work/'proof'
proof.mkdir()
tools = work/'tools'
tools.mkdir()
events = repo/'.build_events.json'
product=os.environ['PRODUCT']
out = {'machine':platform.machine(),'product':product,'cases':{},'errors':{}}
def run(argv, env=None, check=True, cwd=None):
    return subprocess.run(argv, text=True, capture_output=True, check=check, env=env, cwd=cwd)
def git(*args):
    return run(['git',*args]).stdout.strip()
def report():
    (proof/'verdict.json').write_text(json.dumps(out,indent=2)+'\n')
def tool(name, code):
    p=tools/name
    p.write_text(code)
    p.chmod(0o755)
docker=shutil.which('docker')
make=shutil.which('make')
assert docker and make
tool('docker', '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CASE_OUT/docker"\nexec '+docker+' "$@"\n')
# Only unrelated failure cleanup is isolated from the log upload.
tool('make', '#!/bin/bash\nif [[ "$*" == clean || "$*" == bazel-shutdown ]]; then exit 0; fi\nexec '+make+' "$@"\n')
tool('sudo', '#!/bin/bash\n[[ "$1" == rm && "$2" == -rf && "$3" == "$HOME"/* ]]\n')
tool('buildkite-agent', '''#!/usr/bin/env python3
import hashlib,json,os,sys
from pathlib import Path
p=Path(os.environ["CASE_OUT"]);args=sys.argv[1:]
if args[:2]==["artifact","upload"]:
 f=Path(args[2]);b=f.read_bytes();dig=hashlib.sha256(b).hexdigest()
 with (p/"uploads").open("a") as out:out.write(json.dumps([str(f),dig])+"\\n")
 (Path(os.environ["WITNESS"])/dig).write_bytes(b)
elif args[0]=="annotate":
 with (p/"annotations").open("a") as out:out.write(sys.stdin.read())
else: raise SystemExit(2)
''')
(witness:=proof/'uploaded-bytes').mkdir()
(home:=work/'home').mkdir()
base_env=dict(os.environ,HOME=str(home),PATH=str(tools)+':'+os.environ['PATH'],DOCKER_HOST='unix://'+str(work/'unavailable.sock'),SKIP_IMAGE_LOAD='true',WITNESS=str(witness),BUILDKITE_COMMAND='',BUILDKITE_LABEL='fixture',BUILDKITE_JOB_ID='local',BUILDKITE_BUILD_ID='local')
base=git('rev-parse',product+'^')
assert git('rev-parse','HEAD^')==product
f='tools/bazel.mk'
stock=git('show',base+':'+f)+'\n'
fixed=git('show',product+':'+f)+'\n'
assert fixed == stock.replace('filter-out help,','filter-out help testlogs,')
assert git('rev-parse',base+':.buildkite/hooks/post-command')==git('rev-parse',product+':.buildkite/hooks/post-command')
def observe(source,kind,comparison,code):
    path=work/(comparison+'-'+kind);path.mkdir()
    (repo/f).write_text(source)
    env=dict(base_env,CASE_OUT=str(path),BUILDKITE_COMMAND_EXIT_STATUS=str(code))
    if kind=='stock-host':
        env['MAKEFLAGS']='DOCKER_BUILD=false'
    proc=run(['bash','.buildkite/hooks/post-command'],env=env,check=False)
    (path/'hook').write_text(proc.stderr)
    assert proc.returncode==0,(proc.returncode,proc.stderr[-300:])
    calls=(path/'docker').read_text().splitlines() if (path/'docker').exists() else []
    collector=[x for x in calls if x!='ps --format={{.ID}}/{{.Names}}']
    if kind=='stock':
        assert any(x.startswith('exec ') and 'gvisor-bazel-' in x for x in collector),calls
    else:
        assert not collector,calls
    uploads=[json.loads(line) for line in (path/'uploads').read_text().splitlines()] if (path/'uploads').exists() else []
    annotations=(path/'annotations').read_text() if (path/'annotations').exists() else ''
    return {'collectorDocker':collector,'hookCleanupDocker':len(calls)-len(collector),'uploads':uploads,'annotations':annotations}
def check(name, func):
    try:out['cases'][name]=func()
    except Exception as e:out['errors'][name]=f'{type(e).__name__}: {e}'
    report()
def empty():
    assert not events.exists()
    got={k:observe(s,k,'cold',0) for k,s in [('stock',stock),('fixed',fixed)]}
    assert all(not v['uploads'] and not v['annotations'] for v in got.values())
    return got
check('cold-no-event-file',empty)
(repo/f).write_text(fixed)
def pass_only():
    proc=run(['bazel','test','--jobs=4','--strategy=TestRunner=local','--nocache_test_results','--nobuild_event_json_file_path_conversion','--build_event_json_file=.build_events.json','//test/rbe/host_testlogs:pass'],check=False)
    if proc.returncode:
        raise RuntimeError(str(proc.returncode)+' '+proc.stderr[-1700:])
    summaries=[e['testSummary'] for e in map(json.loads, events.read_text().splitlines()) if 'testSummary' in e]
    assert len(summaries)==1 and summaries[0]['overallStatus']=='PASSED' and not summaries[0].get('failed'),summaries
    got={k:observe(s,k,'success',0) for k,s in [('stock',stock),('fixed',fixed)]}
    assert all(not v['uploads'] and not v['annotations'] for v in got.values())
    return got
check('real-passing-test-no-uploads',pass_only)
(repo/f).write_text(fixed)
def build():
    marker=work/'retry-marker'
    labels=['//test/rbe/host_testlogs:'+s for s in ['pass','fail','flaky']]
    args=['bazel','test','--jobs=4','--strategy=TestRunner=local','--nocache_test_results','--flaky_test_attempts=2','--test_output=errors','--nozip_undeclared_test_outputs','--nobuild_event_json_file_path_conversion','--build_event_json_file=.build_events.json','--test_env=HOST_LOG_RETRY_MARKER='+str(marker),*labels]
    proc=run(args,check=False)
    (work/'producer.log').write_text(proc.stdout+proc.stderr)
    if proc.returncode!=3:
        raise RuntimeError(str(proc.returncode)+' '+proc.stderr[-1700:])
    sums={x['id']['testSummary']['label']:x['testSummary'] for x in map(json.loads,events.read_text().splitlines()) if 'testSummary' in x}
    statuses={label:sums[label]['overallStatus'] for label in labels}
    assert list(statuses.values())==['PASSED','FAILED','FLAKY'],statuses
    failed={label:[unquote(urlsplit(e['uri']).path) for e in sums[label].get('failed',[])] for label in labels}
    assert not failed[labels[0]] and all(failed[l] for l in labels[1:]),failed
    attempts={l:[] for l in labels}
    for e in map(json.loads,events.read_text().splitlines()):
        if 'testResult' in e:
            test=e['id']['testResult']
            attempts[test['label']].append([int(test.get('attempt',1)),e['testResult']['status']])
    attempts={l:sorted(v) for l,v in attempts.items()}
    assert list(attempts.values())==[[[1,'PASSED']],[[1,'FAILED'],[2,'FAILED']],[[1,'FAILED'],[2,'PASSED']]],attempts
    b={'producerExit':proc.returncode,'statuses':statuses,'attempts':attempts,'failed':failed}
    (proof/'bazel-results.json').write_text(json.dumps(b,indent=2)+'\n')
    return b
check('actual-bazel-statuses',build)
def logs():
    b=out['cases']['actual-bazel-statuses']
    expected={x for paths in b['failed'].values() for x in paths}
    got={k:observe(s,k,'events',3) for k,s in [('stock',stock),('stock-host',stock),('fixed',fixed)]}
    for result in [got['stock-host'],got['fixed']]:
        paths=[p for p,sha in result['uploads']]
        assert set(p for p in paths if p.endswith('.log'))==expected,paths
        assert 'TestHostLog_fail' in result['annotations'] and 'TestHostLog_flaky' in result['annotations'],result
        assert all('HostLog.'+n in result['annotations'] for n in ['fail','flaky']),result
        assert all(any(p.endswith('fixture-'+n+ext) for p in paths) for n in ['fail','flaky'] for ext in ['.boot.txt','.gofer.txt']),paths
    assert sorted(got['stock-host']['uploads'])==sorted(got['fixed']['uploads']) and got['stock-host']['annotations']==got['fixed']['annotations']
    if got['stock']['uploads']:
        assert sorted(got['stock']['uploads'])==sorted(got['fixed']['uploads']) and got['stock']['annotations']==got['fixed']['annotations']
    return got
check('actual-failure-and-retry-uploads',logs)
(repo/f).write_text(fixed)
out['passed']=not out['errors'] and len(out['cases'])==4
report()
print(json.dumps({'machine':out['machine'],'errors':out['errors'],'cases':list(out['cases'])}))
sys.exit(not out['passed'])
