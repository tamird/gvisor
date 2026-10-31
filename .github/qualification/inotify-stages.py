"""Observe original syscall case, analyzer, or formatter output on Linux."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

root=Path(os.environ['RUNNER_TEMP'])
out=root/'inotify-evidence'
out.mkdir(exist_ok=True)
stage=sys.argv[1]
source=os.environ[{'linux':'SOURCE','stock':'STOCK','fixed':'SOURCE','locks':'LOCKS','kernfs':'SOURCE','format':'SOURCE','owners':'SOURCE','guard':'LOCKS','negative':'MISSING'}[stage]]
cases=['MaskCreatePreservesExistingWatch','HardlinksReuseSameWatch','AddWatchWithInvalidEventMaskFails','DuplicateWatchReturnsSameWatchDescriptor','MaskAddMergesWithExistingEventMask','OneShot']
clean_cases=cases[3:]
rec={'source':source,'stage':stage,'result':'FAIL'}
docker=['docker','exec','-e','GIT_CONFIG_COUNT=1','-e','GIT_CONFIG_KEY_0=safe.directory','-e','GIT_CONFIG_VALUE_0='+os.environ['GITHUB_WORKSPACE'],'overlay-guest']
def get(argv):
    return subprocess.check_output(argv,text=True)
def run():
    subprocess.run(['git','checkout','--quiet','--detach',source],check=True)
    assert get(['git','rev-parse','HEAD']).strip()==source
    rec['kernel']=get(['uname','-r']).strip()
    path='test/syscalls/linux/inotify.cc'
    rec['fixture_sha256']=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if stage=='format':
        p=subprocess.run(['tools/lint.sh','clang-format'],text=True,capture_output=True)
        rec['exit']=p.returncode
        old=Path(path).read_bytes()
        pin=re.search(r'CLANG_FORMAT_VERSION="([^"]+)"',Path('tools/lint.sh').read_text()).group(1)
        formatter=Path(os.environ['HOME'])/'.cache/gvisor/lint'/('clang-format-'+pin)
        if not formatter.exists(): raise RuntimeError((p.stdout+p.stderr)[-2000:])
        rec['version']=get([str(formatter),'--version']).strip()
        new=subprocess.check_output([str(formatter),path])
        if old!=new:
            import difflib
            (out/'canonical-format.patch').write_text(''.join(difflib.unified_diff(old.decode().splitlines(True),new.decode().splitlines(True),fromfile='a/'+path,tofile='b/'+path)))
        assert old==new,(p.stdout+p.stderr)[-1500:]
        return
    is_case=stage in ('linux','stock','fixed','locks')
    is_owner=stage in ('owners','guard','negative')
    args=['--config=x86_64','--remote_download_outputs=toplevel','--nocache_test_results','--test_output=all','--test_sharding_strategy=disabled','--remote_download_regex=.*[/]test[.](log|xml)$']
    if is_case:
        labels=['//test/syscalls:inotify_test_'+('native' if stage=='linux' else 'runsc_ptrace')]
        selected=clean_cases if stage=='locks' else cases
        args+=['--jobs=40','--test_env=GTEST_FILTER='+':'.join('Inotify.'+c for c in selected),'--test_timeout=500','--strategy=TestRunner='+('local' if stage=='linux' else 'remote')]
        if stage!='linux': args+=['--test_arg=--use-tmpfs=true']
    elif stage=='kernfs':
        labels=['//pkg/sentry/fsimpl/kernfs:kernfs_test']
        args+=['--jobs=30','--test_filter=^TestRmdirInotify','--test_arg=-test.v']
    else:
        labels=['//pkg/sentry/vfs:vfs_nogo']
        if stage=='owners': labels+=['//pkg/sentry/syscalls/linux:linux_nogo']
        args+=['--jobs=4','--strategy=GoStaticAnalysis=linux-sandbox','--strategy=GoStandardLibraryAnalysis=linux-sandbox','--strategy=GoStdlib=linux-sandbox','--strategy=GoStdlibList=linux-sandbox','--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache']
    args = [arg for arg in args if not arg.startswith('--jobs=')] + ['--jobs=8','--strategy=GoStaticAnalysis=linux-sandbox','--strategy=GoStandardLibraryAnalysis=linux-sandbox','--strategy=GoStdlib=linux-sandbox','--strategy=GoStdlibList=linux-sandbox','--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache']
    rec['labels']=labels
    bep=root/(stage+'.bep');log=root/(stage+'.bazel.log')
    with log.open('w') as f:
        rc=subprocess.run(docker+['bazel','--bazelrc='+str(root/'overlay.bazelrc'),'test','--build_event_json_file='+str(bep)]+args+labels,stdout=f,stderr=subprocess.STDOUT).returncode
    rec['exit']=rc
    body=log.read_text(errors='replace')
    rec['invocations']=sorted(set(re.findall(r'https://app.buildbuddy.io/invocation/[a-f0-9-]+',body)))
    event=[]
    if bep.exists():
        with bep.open() as f:
            for line in f:
                e=json.loads(line)
                if any(e.get('id',{}).get(k,{}).get('label') in labels for k in ('testSummary','testResult','targetCompleted')): event.append(e)
    (out/(stage+'-events.json')).write_text(json.dumps(event))
    sums={e['id']['testSummary']['label']:e['testSummary']['overallStatus'] for e in event if 'testSummary' in e.get('id',{})}
    rec['summaries']=sums
    logs={}
    for label in labels:
        p='bazel-testlogs/'+label[2:].replace(':','/')
        lg=subprocess.run(docker+['cat',p+'/test.log'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True).stdout
        logs[label]=lg
        if lg: (out/(stage+'-'+label.split(':')[1]+'.log')).write_text(lg)
        if is_case:
            raw=subprocess.run(docker+['cat',p+'/test.xml'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout
            if not raw: continue
            (out/(stage+'-test.xml')).write_bytes(raw)
            actual={}
            for case in ET.fromstring(raw).iter('testcase'):
                name=case.get('classname','')+'.'+case.get('name','')
                errors=case.findall('error')+case.findall('failure')
                actual[name]={'outcome':'SKIP' if case.find('skipped') is not None or case.get('status')=='notrun' or case.get('result') in ('skipped','suppressed') else 'FAIL' if errors else 'PASS','message':' '.join((x.text or x.get('message','')) for x in errors)[:4500]}
            rec['cases']=actual
    if rc!=0:
        # Never include secret-bearing command lines in a result bundle.
        rows=[s for s in body.splitlines() if re.search(r'ERROR:|inotify\.go:|FAILED:|FAILED TO BUILD|remote cache.*error',s,re.I) and not re.search('header|secret|key=|token',s,re.I)]
        (out/(stage+'-diagnosis.txt')).write_text('\n'.join(rows[-35:])[-6000:])
    if is_case:
        actual=rec.get('cases',{})
        assert set(actual)=={'Inotify.'+c for c in selected},(set(actual),selected)
        for pos,c in enumerate(selected):
            r=actual['Inotify.'+c]
            want='FAIL' if stage=='stock' and pos<3 else 'PASS'
            assert r['outcome']==want,(c,want,r)
            if want=='FAIL':
                assert re.search(r'EEXIST|File exists|errno 17' if pos<2 else r'EINVAL|Invalid argument|errno 22',r['message'],re.I),r
        assert rc==(3 if stage=='stock' else 0),rc
    elif stage=='kernfs':
        passed=re.findall(r'^--- PASS: (TestRmdirInotify\w+) ',list(logs.values())[0],re.M)
        assert rc==0 and len(set(passed))==2,(rc,passed)
        rec['passed']=passed
    elif is_owner and stage=='negative':
        d='\n'.join(list(logs.values())+[body])
        rec['guard_findings']=[l[:420] for l in d.splitlines() if 'inotify.go:151:' in l and 'must hold' in l and 'to call remove' in l and 'not held' in l]
        assert rc!=0 and rec['guard_findings'],(rc,sums)
    else:
        assert rc==0 and sums=={l:'PASSED' for l in labels},(rc,sums)
try:
    run()
    rec['result']='PASS'
except BaseException as e:
    rec['error']=repr(e)[:3000]
    raise
finally:
    (out/(stage+'.json')).write_text(json.dumps(rec,indent=2)+'\n')
    print(stage,rec['result'],rec.get('exit'),rec.get('invocations'),rec.get('error',''),flush=True)
