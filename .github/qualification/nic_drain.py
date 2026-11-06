"""Retain the two observed deadlock sites and native corrected test results."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse

ROOT=Path(os.environ['RUNNER_TEMP'])/'nic-drain'
OUT=ROOT/'results'
BASE='f51a205a8f4afbc5ab41aded070df74951b081ea'
FIX='45af0a2533d2e46a6ebb7120c8649c37222ede84'
TEST=Path('pkg/tcpip/stack/stack_test.go')
STACK='//pkg/tcpip/stack:stack_x_test'
NOGO='//pkg/tcpip/stack:stack_nogo'
TARGET='//pkg/tcpip/adapters/gonet:qualification_tcp_stream'
REG='TestStackWaitAllowsReceiveLookup'
CASES=[REG,'TestRemoveNIC','TestNICMTULimitsSurviveMove','TestRemoveRoutes','TestNICContextPreservation','TestNICKindPreservation']
COMMON=['--config=x86_64','--lockfile_mode=off','--jobs=4','--local_resources=memory=8192',
        '--strategy=GoStdlib=processwrapper-sandbox','--strategy=GoStdlibList=processwrapper-sandbox',
        '--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox',
        '--remote_executor=','--remote_cache=','--bes_backend=','--spawn_strategy=linux-sandbox',
        '--strategy=TestRunner=linux-sandbox','--sandbox_default_allow_network=false','--nocache_test_results',
        '--test_sharding_strategy=disabled','--flaky_test_attempts=1','--test_env=GO_TEST_WRAP_TESTV=1',
        '--test_timeout=180','--test_output=errors','--color=no','--curses=no','--noshow_progress']


def git(*args):
    return subprocess.check_output(['git',*map(str,args)],text=True).strip()


def save(info):
    (OUT/'results.json').write_text(json.dumps(info,indent=2)+'\n')


def run(info,name,target,flags):
    bep=ROOT/(name+'.bep')
    console=ROOT/(name+'.out')
    cmd=[str(ROOT/'bazelisk'),'--nosystem_rc','--nohome_rc','test',*COMMON,*flags,'--build_event_json_file='+str(bep),target]
    with console.open('wb') as log: rc=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT).returncode
    result={'exit':rc,'target':target,'source':git('rev-parse','HEAD'),'records':[]}
    texts=[]
    if bep.exists():
        for line in bep.open():
            ev=json.loads(line)
            if ev['id'].get('testResult',{}).get('label')!=target: continue
            t=ev['testResult']
            rec={'status':t.get('status'),'strategy':t.get('executionInfo',{}).get('strategy'),'cached':t.get('cachedLocally',False)}
            entry,=[x for x in t['testActionOutput'] if x['name']=='test.log']
            u=urllib.parse.urlsplit(entry['uri']);assert u.scheme=='file'
            data=Path(urllib.parse.unquote(u.path)).read_bytes()
            texts.append(data.decode(errors='replace'))
            rec['sha256']=hashlib.sha256(data).hexdigest()
            logname=name+'-'+str(len(texts))+'.log'
            (OUT/logname).write_bytes(data if len(data)<=40000 else data[:20000]+b'\n...\n'+data[-20000:])
            result['records'].append(rec)
    if not texts: result['console_end']=console.read_bytes()[-8000:].decode(errors='replace')
    text='\n'.join(texts)
    result['passed_cases']=sorted(set(re.findall(r'--- PASS: (Test[A-Za-z0-9_]+)(?=\s)',text)))
    if name=='stock':
        result['timeout_sites']=sorted(set(re.findall(r'receive worker blocked on NIC lookup during (detach|wait)',text)))
    if name=='nogo': result['arches']=[arch for arch in ('amd64','arm64') if arch+'...PASS' in text]
    if name=='wire': result['verified']=text.count('transfer verified')
    info[name]=result;save(info);print(name,rc,result['records'],result['passed_cases'],flush=True)


def main():
    assert git('rev-parse','HEAD^')==FIX and git('rev-parse',FIX+'^')==BASE
    git('checkout','-q','--detach',BASE)
    git('restore','--source='+FIX,'--worktree','--',TEST)
    assert git('diff','--name-only','HEAD')==str(TEST)
    info={'product':FIX,'base':BASE,'status':'INCOMPLETE'};save(info)
    run(info,'stock',STACK,['--test_filter=^'+REG+'$','--test_arg=-test.v','--test_arg=-test.timeout=70s'])
    git('restore','--worktree','--source=HEAD','--',TEST)
    git('checkout','-q','--detach',FIX)
    assert not git('status','--porcelain')
    tests='^('+'|'.join(CASES)+')$'
    run(info,'regular',STACK,['--test_filter='+tests,'--test_arg=-test.v'])
    run(info,'nogo',NOGO,[])
    run(info,'race',STACK,['--config=race','--test_filter='+tests,'--test_arg=-test.v'])
    pkg=Path('pkg/tcpip/adapters/gonet')
    bench=pkg/'qualification_tcp_read_test.go'
    shutil.copyfile(ROOT/'nic_stream.go',bench)
    with (pkg/'BUILD').open('a') as b:
        b.write("""
go_test(
    name = "qualification_tcp_stream",
    size = "medium",
    srcs = ["qualification_tcp_read_test.go"],
    embed = [":gonet"],
    deps = [
        "//pkg/tcpip", "//pkg/tcpip/header", "//pkg/tcpip/link/fdbased",
        "//pkg/tcpip/network/ipv4", "//pkg/tcpip/stack", "//pkg/tcpip/transport/tcp",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
""")
    cpus=','.join(str(i) for i in sorted(os.sched_getaffinity(0))[:2])
    run(info,'wire',TARGET,['--run_under=/usr/bin/taskset -c '+cpus,'--test_env=TCP_BENCH_CPUS='+cpus,
            '--test_env=GOMAXPROCS=2','--test_arg=-test.run=^TestConnectedTCPReadTiming$','--test_arg=-test.v',
            '--test_arg=-test.count=8','--test_arg=-test.timeout=90s'])
    assert info['stock']['exit']!=0 and info['stock']['timeout_sites']==['detach','wait']
    for lane in ('regular','nogo','race','wire'):
        row=info[lane]
        assert row['exit']==0 and row['records'] and all(x['status']=='PASSED' and not x['cached'] for x in row['records'])
    assert set(info['regular']['passed_cases'])==set(info['race']['passed_cases'])==set(CASES)
    assert info['nogo']['arches']==['amd64','arm64'] and info['wire']['verified']==16
    info['status']='PASS';save(info)


if __name__=='__main__': main()
