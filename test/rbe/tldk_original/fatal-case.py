"""Run the original Go test ELF through Bazel; save only scoped observations."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

temp=Path(os.environ['RUNNER_TEMP']);out=temp/'receipt';install=temp/'reused'
assert json.loads((out/'original-actions.json').read_text())['status']=='PASS'
assert json.loads((temp/'traffic/device.json').read_text())['status']=='PASS'
out.mkdir(exist_ok=True);shutil.copyfile(temp/'traffic/device.json',out/'device.json')
def run(*args,**kwargs):return subprocess.run(args,check=True,**kwargs)
image='ubuntu@sha256:64483f3496c1373bfd55348e88694d1c4d0c9b660dee6bfef5e12f43b9933b30'
run('docker','pull',image);run('docker','tag',image,'gvisor.dev/images/basic/ubuntu')
run('docker','run','--rm','--entrypoint','/bin/sh',image,'-c','set -e; command -v nc; command -v awk; command -v grep')
pkg=Path('test/rbe/tldk_original');pkg.mkdir(exist_ok=True,parents=True)
shutil.copy2(install/'original-test',pkg/'exact_e2e')
(pkg/'BUILD').write_text('load("@bazel_skylib//rules:native_binary.bzl", "native_test")\n\nnative_test(name="traffic",src="exact_e2e",size="large",local=True)\n')
modes={'plugin':'qualification-plugin-debug'}
logs=temp/'tldk-private-logs'
for mode in modes:
 d=logs/mode;d.mkdir(parents=True,exist_ok=True)
 run('sudo',install/'runsc','install','--runtime='+modes[mode],'--download-sidecars=NEVER','--','--network='+('plugin' if mode=='plugin' else 'sandbox'),'--platform=systrap','--debug','--debug-log='+str(d)+'/', '--panic-log='+str(d/'runtime-panic.log'))
run('sudo','systemctl','reload','docker')
runtime=json.loads(subprocess.check_output(['sudo','cat','/etc/docker/daemon.json']))['runtimes']
for mode in modes:
 x=runtime[modes[mode]];assert x['path']==str(install/'runsc') and '--network='+('plugin' if mode=='plugin' else 'sandbox') in x['runtimeArgs']
rec={'source':os.environ['PRODUCT'],'testSha256':hashlib.sha256((pkg/'exact_e2e').read_bytes()).hexdigest(),'image':image,'outcomes':{}}
def flush(): (out/'diagnosis.json').write_text(json.dumps(rec,indent=2)+'\n')
def safe(line):
 s=line.strip()
 if len(s)>2800 or re.search(r'\b(env|envs|environ|environment|args|cmdline|command.line|token|password|secret|authorization|credential|cookie|bearer|api.key|syscall.*0x.*0x.*0x)\b',s,re.I):return ''
 s=re.sub(r'/home/runner/\S+','<runner-path>',s)
 s=re.sub(r'\b[0-9a-f]{12,}\b','<id>',s,flags=re.I)
 s=re.sub(r'(?<!\w)(?:\d{1,3}\.){3}\d{1,3}(?:/\d+)?(?!\w)','<ip>',s)
 s=re.sub(r'\b0x[0-9a-fA-F]+\b','<address>',s);s=re.sub(r'https?://\S+','<url>',s)
 return s[:450]
def diagnostics(mode):
 if mode=='native':return
 # Do not retain raw debug/daemon logs: only actual fatal/warning symbols and
 # function names relevant to the process that failed to start are public.
 terms=('panic:','fatal error:','unexpected signal','SIGSYS','SIGSEGV','SIGABRT','SIGBUS','SIGILL','SIGKILL','seccomp violation','stack trace','permission denied','failed to ','operation not permitted','bad system call','fatal:','eal:','assertion','exited with','exit status','unsupported syscall','unexpected trap','aborted','segmentation fault','signal: killed','invalid argument','failed:','cannot ')
 summaries=[]
 for p in sorted((logs/mode).iterdir()):
  if not p.is_file():continue
  data=subprocess.check_output(['sudo','tail','-c','1000000',p]).decode(errors='replace').splitlines()
  hits=[]
  for i,line in enumerate(data):
   if not any(t.lower() in line.lower() for t in terms):continue
   c=safe(line)
   if not c or c in hits:continue
   hits.append(c)
   for nextline in data[i+1:i+25]:
    # Only function names, never argument values or stack memory.
    m=re.match(r'^(?:\s*)(([a-zA-Z0-9_.~/-]+\.)+[a-zA-Z][\w.]*|[a-zA-Z0-9_.~/-]+\.\(\*\w+\)\.\w+)\(',nextline)
    if m and m.group(1) not in hits:hits.append(m.group(1))
  if hits:summaries.append({'kind':re.sub(r'[0-9]+','N',p.name)[-90:],'findings':hits[:12]+(hits[-8:] if len(hits)>12 else [])})
 (out/(mode+'-excerpt.json')).write_text(json.dumps(summaries,indent=2)+'\n')
def test(mode,rt):
 p=temp/(mode+'.bep');log=temp/(mode+'.build-log')
 cmd=['bazelisk','test','--noremote_accept_cached','--spawn_strategy=local','--strategy=TestRunner=local','--nocache_test_results','--test_sharding_strategy=disabled','--test_timeout=120','--test_output=all','--test_arg=-test.v','--test_arg=-test.run=^TestConnectToSelf$','--test_arg=-runtime='+rt,'--build_event_json_file='+str(p),'//test/rbe/tldk_original:traffic']
 with log.open('w') as f:res=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT)
 row=rec['outcomes'][mode]={'bazelExit':res.returncode}
 if p.exists():
  for l in p.open():
   e=json.loads(l)
   if 'testResult' in e:
    t=e['testResult'];row['result']=t.get('status');row['execution']=t.get('executionInfo',{});row['cached']=t.get('cachedLocally',False)
 tl=Path('bazel-testlogs/test/rbe/tldk_original/traffic/test.log')
 actual=tl.read_text(errors='replace').replace('\x16','') if tl.exists() else ''
 row['namedStarts']=re.findall(r'^\s*=== RUN\s+(\S+)',actual,re.M)
 row['namedPasses']=re.findall(r'^\s*--- PASS:\s+(\S+)',actual,re.M)
 row['namedFailures']=re.findall(r'^\s*--- FAIL:\s+(\S+)',actual,re.M)
 row['skipped']=bool(re.search(r'^\s*--- SKIP: ',actual,re.M))
 row['namedFailureMessages']=[c for l in actual.splitlines() if 'integration_test.go:' in l and (c:=safe(l))][:8]
 if not row['namedStarts']:row['buildDiagnostics']=[c for l in log.read_text(errors='replace').splitlines() if 'ERROR:' in l and (c:=safe(l))][-6:]
 diagnostics(mode);flush();print(mode,row,flush=True)
import time
for phase in ('path',):
 proc=subprocess.Popen(['sudo','env','RUNNER_TEMP='+str(temp),'python3',temp/'fatal-secc.py'])
 try:
  for _ in range(800):
   if (temp/(phase+'.ready')).exists() or proc.poll() is not None:break
   time.sleep(.1)
  assert (temp/(phase+'.ready')).exists() and proc.poll() is None,phase
  test('plugin',modes['plugin'])
  shutil.copyfile(out/'diagnosis.json',out/(phase+'-case.json'))
 finally:
  (temp/(phase+'.stop')).touch()
  assert proc.wait(timeout=65)==0,phase
 row=rec['outcomes']['plugin'];assert row['namedFailures']==['TestConnectToSelf'] and not row['skipped'],row
 assert any('containerManager.StartRoot' in x for x in row['namedFailureMessages']),row
# A genuine negative is reported as observed, not represented as a successful exchange.
for mode,row in rec['outcomes'].items():
 assert row['namedStarts']==['TestConnectToSelf'] and not row['skipped'],(mode,row)
 assert row['execution'].get('strategy') in ('local','standalone') and not row['cached'] and not row['execution'].get('cachedRemotely'),(mode,row)
 assert (row['result'],row['namedPasses'],row['namedFailures']) in [('PASSED',['TestConnectToSelf'],[]),('FAILED',[],['TestConnectToSelf'])],(mode,row)
assert rec['outcomes']['plugin']['result']=='FAILED'
obs=json.loads((out/'denied-openat.json').read_text());assert obs['audit'] and any(r['registerSyscall']==257 and r['pathReadResult']>0 and r['syscallSite']['elfOffset']==obs['priorELFOffset'] for r in obs['fatal']),obs
rec['qualification']='originals and one executed case verified; process observations recorded separately';flush()
