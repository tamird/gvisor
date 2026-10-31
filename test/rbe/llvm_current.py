"""Verify the configured compiler and its actual users without changing the source."""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

temp = Path(os.environ['RUNNER_TEMP'])
out = temp / 'compiler-owners'
out.mkdir(exist_ok=True)
bazel = ['bazelisk', '--bazelrc=' + str(temp / 'images.bazelrc')]
product = os.environ['PRODUCT']
targets = {
    'test_app': '//test/cmd/test_app:test_app',
    'ioctl_hook': '//tools/ioctl_sniffer:ioctl_hook_gnu',
    'sniffer': '//tools/ioctl_sniffer:run_sniffer',
    'sidecar': '//runsc/fdparking:fdparking_binary',
    'runsc': '//runsc:runsc',
    'sentry': '//runsc/cmd/sentry:gvisor_sentry',
}
plugin_targets = {
    'runsc_plugin': '//runsc:runsc-plugin-stack',
    'sentry_plugin': '//runsc/cmd/sentry:gvisor_sentry_plugin_stack',
}
parser_targets = {'parser':'//tools/nvidia_driver_differ:driver_ast_parser'}
cfg = {'amd64': 'x86_64', 'arm64': 'aarch64'}
expected_machine = {'amd64': 62, 'arm64': 183}
stages = {}
report = {'product': product, 'sourceTree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],text=True).strip(),
          'host':os.uname().machine,'stages':stages}
assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip() == product

def flush():
    (out/'verdict.json').write_text(json.dumps(report,indent=2)+'\n')

def action(name,fn):
    row = stages[name] = {'status':'RUNNING'}
    print(f'::group::{name}',flush=True)
    try:
        fn(row)
    except Exception as exc:
        row.update(status='FAIL',error=repr(exc))
    else:
        row['status'] = 'PASS'
    finally:
        flush()
        print(f'{name}: {row["status"]} {row.get("error","")}',flush=True)
        print('::endgroup::',flush=True)

def baz(row,name,command,*args,queries=False):
    bep = temp/(name+'.bep')
    log = temp/(name+'.log')
    # The RBE header itself is not an artifact. Keep only error lines, original
    # test logs, named output labels and invocation IDs in the evidence bundle.
    argv = [*bazel,command,'--color=no','--curses=no', '--build_event_json_file='+str(bep),*args]
    with log.open('w') as stream:
        res = subprocess.run(argv,stdout=stream,stderr=subprocess.STDOUT)
    records = row.setdefault('invocations',[])
    saved = {'name':name,'command':command,'arguments':list(args),'exit':res.returncode}
    records.append(saved)
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                e=json.loads(line)
                if 'started' in e:
                    saved['invocation']=e['started']['uuid']
                label=e.get('id',{}).get('targetCompleted',{}).get('label')
                if label and label in args:
                    saved.setdefault('requestedTargetResults',{})[label]=e.get('completed',{}).get('success',False)
                if label and not e.get('completed',{}).get('success',False):
                    saved.setdefault('unsuccessfulTargets',[]).append(label)
                if 'testSummary' in e:
                    saved.setdefault('tests',{})[e['id']['testSummary']['label']]=e['testSummary'].get('overallStatus')
                if 'testResult' in e:
                    q=e['testResult']
                    saved.setdefault('actualTestResults',[]).append({'label':e['id']['testResult']['label'],'status':q['status'],'cachedLocally':q.get('cachedLocally',False),'executionInfo':q.get('executionInfo',{})})
    diag = []
    safeWords=('error:','fatal:','undefined reference',' killed','could not','failure','missing input','not found','no such file','remote invocation','finished after','build completed','critical path','processes:')
    for line in log.read_text(errors='replace').splitlines():
        s=line.lower()
        if any(w in s for w in safeWords) and not any(x in s for x in ('remote_header','bes_header','api-key','github_token','buildbuddy_api_key','authorization:', 'bearer ')):
            diag.append(line[-1200:])
    (out/(name+'.diagnostics')).write_text('\n'.join(diag[:10]+diag[-45:])+'\n')
    flush()
    if res.returncode:
        print('\n'.join(diag[-10:]),flush=True)
        raise RuntimeError(f'{command} returned {res.returncode}; {name}')
    return saved

def invocation_path(arch,label,extra=()):
    cmd=[*bazel,'cquery','--noshow_progress','--config='+cfg[arch],*extra,'--output=files',label]
    p = subprocess.run(cmd,text=True,capture_output=True)
    if p.returncode:
        raise RuntimeError(f'cquery {label} failed {p.returncode}: '+p.stderr[-350:])
    files=[Path(x) for x in p.stdout.splitlines() if x.strip()]
    assert len(files)==1 and files[0].is_file(),(label,files)
    return files[0]

def elf(arch,p):
    raw=p.read_bytes()
    assert raw[:6]==b'\x7fELF\x02\x01' and int.from_bytes(raw[18:20],'little')==expected_machine[arch],p
    ph=subprocess.check_output(['readelf','-lW',p],text=True)
    dynamic=subprocess.check_output(['readelf','-dW',p],text=True)
    return {'file':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'e_machine':expected_machine[arch],
            'interpreter':next((s.strip() for s in ph.splitlines() if 'Requesting program interpreter' in s),None),
            'needed':re.findall(r'\(NEEDED\).*?\[(.*?)\]',dynamic)}

def binaries(row,arch,members,extra=()):
    try:
        baz(row,arch+'-'+next(iter(members)),'build','--config='+cfg[arch],*extra,'--keep_going',*members.values())
    except RuntimeError as exc:
        failure=exc
    else:
        failure=None
    success=row['invocations'][-1].get('requestedTargetResults',{})
    files={}
    for short,label in members.items():
        if failure and not success.get(label):
            continue
        p=files[short]=invocation_path(arch,label,extra)
        e=row.setdefault('outputs',{})[short]=elf(arch,p)
        if short in ('sidecar','test_app','runsc_plugin','sentry_plugin'):
            assert e['interpreter'] is None and not e['needed'],(short,e)
        if short == 'ioctl_hook':
            assert 'libc.so.6' in e['needed'],e
    if 'sniffer' in files and 'ioctl_hook' in files:
        assert files['ioctl_hook'].read_bytes() in files['sniffer'].read_bytes()
        row['completeGnuHookEmbedded']=True
    if failure:
        raise failure

def records(path):
    data=path.read_text().strip()
    if data.startswith('['):return json.loads(data)
    reader=json.JSONDecoder()
    res=[];p=0
    while p<len(data):
        while p<len(data) and data[p].isspace():p+=1
        if p<len(data):
            item,p=reader.raw_decode(data,p);res.append(item)
    return res

def allocator(row,arch):
    f=temp/(arch+'.exec.json')
    label='//test/util:errno_safe_allocator'
    baz(row,arch+'-allocator','build','--config='+cfg[arch],'--cxxopt=-Werror','--execution_log_json_file='+str(f),label)
    match=[q for q in records(f) if q.get('mnemonic')=='CppCompile' and any(str(a).endswith('test/util/errno_safe_allocator.cc') for a in q.get('commandArgs',[]))]
    assert len(match)==1,(arch,len(match))
    q=match[0];args=q['commandArgs']
    assert 'llvm' in args[0] and args[0].endswith('clang++') and '-Werror' in args,args[:6]
    assert q.get('exitCode',0)==0
    row['compile']={'compiler':args[0],'targetFlags':[arg for i,arg in enumerate(args) if 'target=' in arg or 'sysroot=' in arg or i and args[i-1]=='-target'],'runner':q.get('runner'),'cacheHit':q.get('cacheHit',False),'exit':q.get('exitCode',0)}
    assert ('aarch64' if arch=='arm64' else 'x86_64') in ' '.join(row['compile']['targetFlags']),row['compile']

def test(row,name,label,testlog,opts=(),cases=(),archs=False):
    info=baz(row,name,'test','--config=x86_64','--nocache_test_results','--test_output=errors','--test_sharding_strategy=disabled',*opts,label)
    assert info['tests']=={label:'PASSED'} and len(info['actualTestResults'])==1,info
    rec=info['actualTestResults'][0]
    assert rec['status']=='PASSED' and not rec['cachedLocally'] and not rec['executionInfo'].get('cachedRemotely'),rec
    log=Path('bazel-testlogs',testlog,'test.log').read_bytes().replace(b'\x16',b'')
    (out/(name+'.test.log')).write_bytes(log)
    if archs:
        for a in ('amd64','arm64'):assert re.search(rf'(?m)^{a}\.\.\.PASS\s*$',log.decode(errors='replace')),(a,log[-700:])
    for case in cases:
        assert re.search(rb'^\s*=== RUN\s+'+case+rb'\s*$',log,re.M)
        assert re.search(rb'^\s*--- PASS: '+case+rb'\s',log,re.M)
    assert not re.search(rb'^\s*--- (?:FAIL|SKIP): ',log,re.M)

for arch in cfg:
    action(arch+'-strict-C++',lambda row,a=arch:allocator(row,a))
    action(arch+'-core-binaries',lambda row,a=arch:binaries(row,a,targets))
action('analyze-static-Go',lambda row:test(row,'static-go-owner','//test/cmd/test_app:test_app_nogo','test/cmd/test_app/test_app_nogo',archs=True))
action('plugin-diagnostics',lambda row:test(row,'musl-stack','//external/tools/plugin-stack:backtrace_test','external/tools/plugin-stack/backtrace_test',('--config=plugin-tldk','--test_filter=^TestDumpStack$','--test_env=GO_TEST_WRAP_TESTV=1','--test_arg=-test.v'),[b'TestDumpStack']))
action('amd64-plugin-consumers',lambda row:binaries(row,'amd64',plugin_targets,('--config=plugin-tldk',)))
for arch in cfg:
    action(arch+'-LLVM-source-parser',lambda row,a=arch:binaries(row,a,parser_targets))
report['status']='PASS' if all(s['status']=='PASS' for s in stages.values()) else 'FAIL'
flush()
sys.exit(report['status']!='PASS')
