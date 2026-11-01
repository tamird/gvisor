"""Compare Make's actual argv against the prior, executed race smoke."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

out = Path(os.environ['RUNNER_TEMP']) / 'race-callers'
out.mkdir(exist_ok=True)
product = os.environ['PRODUCT']
prior = '8c6ed7a88a418240a629c29a6e9bbe217491bbab'
target = '//runsc:runsc-race'
report = {'source':product, 'prior':prior, 'conclusion':'INCOMPLETE', 'mode':'GNU Make argv capture; no Bazel execution'}

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

try:
    assert git('rev-parse','HEAD') == product
    assert git('diff','--name-only',prior,product).splitlines() == ['tools/bazel.mk']
    diff = git('diff','--unified=0',prior,product,'--','tools/bazel.mk')
    assert [l for l in diff.splitlines() if l.startswith(('+\t','-\t'))] == ['-\t@$(call build,--config=race runsc:runsc-race)','+\t@$(call build,//runsc:runsc-race)']
    raw = subprocess.check_output(['gh','api','repos/tamird/gvisor/actions/artifacts/13069493542/zip'])
    assert hashlib.sha256(raw).hexdigest() == '3c898457e88d1dfa579f99cd9d255d8ccb7a5a057b4a966eb911403a73318efe'
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        old = json.loads(z.read('result.json'))
    assert old['source']==prior and old['conclusion']=='PASS' and old['makeExit']==0 and old['matchedStamp']==prior[:12]
    assert len(old['stagedProducts'])==8 and old['guestUid']==1001 and old['glibcTunables']=='glibc.pthread.rseq=0'
    original = [l.split() for l in old['bazel-argv.log'] if l.startswith('build ')]
    assert [args[-1] for args in original] == ['//:release',target]
    report['priorMake'] = {'source':prior,'buildArgv':original,'guest':old['guest'],'stamp':old['matchedStamp'],'raceLauncherSha256':old['stagedProducts']['runsc']['sha256'],'release':old['builds']['release']['invocation'],'race':old['builds']['race']['invocation']}
    capture = out/'argv'; recorder = out/'bazel'
    recorder.write_text('''#!/usr/bin/python3
import json,os,sys
with open(os.environ['ARGV'], 'a') as f: print(json.dumps(sys.argv[1:]),file=f)
# Stop before Make would stage or run the guest. Its real exit was measured
# in the matching prior command. The developer shortcut can also trace cquery.
if os.environ['MODE']=='smoke-race-tests' and sys.argv[1]=='build' and sys.argv[-1]=='//runsc:runsc-race':sys.exit(75)
''');recorder.chmod(0o755)
    for goal in ['runsc-race','smoke-race-tests']:
        capture.write_text('');env={k:v for k,v in os.environ.items() if k not in ('GH_TOKEN','GITHUB_TOKEN')}
        env.update(ARGV=str(capture),MODE=goal,RUNTIME_DIR=str(out/'runtime'))
        run=subprocess.run(['make','DOCKER_BUILD=false','DOCKER_CLI_PATH=/bin/false','BAZEL='+str(recorder),'BAZEL_OPTIONS=--config=x86_64',goal],capture_output=True,text=True,env=env,timeout=60)
        lines=[json.loads(l) for l in capture.read_text().splitlines()]
        actions=[a for a in lines if a[0]=='build'];queries=[a for a in lines if a[0]=='cquery']; assert not any('--config=race' in a for a in lines)
        if goal=='runsc-race':
            assert run.returncode==0 and actions==[original[-1]] and len(queries)==1 and queries[0][-1]==target
        else:
            assert run.returncode==2 and 'Error 75' in run.stderr and actions==original and len(queries)==1
        report[goal]={'bazelArgv':lines,'makeExit':run.returncode,'interceptedBeforeGuest':goal=='smoke-race-tests'}
    report['conclusion']='PASS'
finally:
    (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
