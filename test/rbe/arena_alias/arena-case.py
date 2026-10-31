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
modes={'original':'qualification-arena-original','configured':'qualification-arena-configured'}
launchers={'original':install/'runsc','configured':temp/'arena-fixed/runsc'}
logs=temp/'tldk-private-logs'
for mode in modes:
 d=logs/mode;d.mkdir(parents=True,exist_ok=True)
 run('sudo',launchers[mode],'install','--runtime='+modes[mode],'--download-sidecars=NEVER','--','--network=plugin','--platform=systrap','--debug','--debug-log='+str(d)+'/', '--panic-log='+str(d/'runtime-panic.log'))
run('sudo','systemctl','reload','docker')
runtime=json.loads(subprocess.check_output(['sudo','cat','/etc/docker/daemon.json']))['runtimes']
for mode in modes:
 x=runtime[modes[mode]];assert x['path']==str(launchers[mode]) and '--network=plugin' in x['runtimeArgs']
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
 flush();print(mode,row,flush=True)

import time
import statistics
ref=(install/'gvisor-bin/gvisor_sentry_plugin_stack');stat=ref.stat()
assert hashlib.sha256(ref.read_bytes()).hexdigest()=='7a272c8e0d4556b0879d8e65a6af9fd1865bfc5ef947dcbd7b15ebe8b78ac32a'
f=(temp/'arena-fixed/gvisor-bin/gvisor_sentry_plugin_stack').stat()
assert (f.st_dev,f.st_ino)==(stat.st_dev,stat.st_ino)
rec['launchers']={m:hashlib.sha256(f.read_bytes()).hexdigest() for m,f in launchers.items()}
assert len(set(rec['launchers'].values()))==2
marker=b'MALLOC_ARENA_MAX='
assert marker in launchers['configured'].read_bytes()
rec['base']='675d49b1c';rec['review']=os.environ['REVIEW'];rec['originalSentrySha256']=hashlib.sha256(ref.read_bytes()).hexdigest()
rec['onlineCPUs']=Path('/sys/devices/system/cpu/online').read_text().strip()
rec['launcherAffinity']=sorted(os.sched_getaffinity(0))
rec['intendedArenaLimit']=8*len(rec['launcherAffinity'])
rec['trials']=[]
def ns(t):return str(t//10**9)+'.'+str(t%10**9).zfill(9)
def observe_docker(t0,t1):
 a=subprocess.run(['docker','events','--since='+str(t0//10**9-1),'--until='+str(t1//10**9+1),'--filter','type=container','--format','{{json .}}'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=12,check=True)
 rows={}
 for line in a.stdout.splitlines():
  e=json.loads(line);action=e.get('Action',e.get('status'));ts=e.get('timeNano',e.get('TimeNano'))
  if action in ('create','start') and ts and t0<=ts<=t1:
   idx=e.get('id') or e['Actor']['ID'];rows.setdefault(idx,{})[action]=ts
 return [{'createToStartMs':(v['start']-v['create'])/10**6 if 'create' in v and 'start' in v else None,'observed':list(v)} for v in rows.values()]
proc=subprocess.Popen(['sudo','env','RUNNER_TEMP='+str(temp),'python3',temp/'arena-audit.py','site'])
try:
 for _ in range(150):
  if (temp/'site.ready').exists() or proc.poll() is not None:break
  time.sleep(.1)
 assert (temp/'site.ready').exists() and proc.poll() is None
 for pair in range(2):
  for mode in (('original','configured') if pair%2==0 else ('configured','original')):
   trial=f'{pair+1}-{mode}';(temp/'arena-trial').write_text(trial)
   t0=time.time_ns();test(mode,modes[mode]);t1=time.time_ns()
   row=dict(rec['outcomes'][mode]);row.update(pair=pair+1,mode=mode,trial=trial,wallMs=(t1-t0)/10**6)
   row['docker']=observe_docker(t0,t1);rec['trials'].append(row);flush()
   assert row['namedStarts']==['TestConnectToSelf'] and not row['skipped'] and not row['cached'] and not row['execution'].get('cachedRemotely') and row['execution'].get('strategy') in ('local','standalone'),row
   assert (row['result'],row['namedPasses'],row['namedFailures']) in [('PASSED',['TestConnectToSelf'],[]),('FAILED',[],['TestConnectToSelf'])],row
finally:
 (temp/'site.stop').touch();assert proc.wait(timeout=30)==0
k=json.loads((out/'site-kernel.json').read_text())
for r in rec['trials']:
 r['fatalOpen']=sum(k['processes'].get(str(x['pid']),{}).get('trial')==r['trial'] and (x.get('site') or {}).get('fileOffset')=='0xe89b3a' for x in k['audits'])
rec['results']={}
for m in modes:
 rows=[x for x in rec['trials'] if x['mode']==m];success=[x for x in rows if x['result']=='PASSED']
 timing=[d['createToStartMs'] for x in success for d in x['docker'] if d['createToStartMs'] is not None]
 rec['results'][m]={'executions':len(rows),'twoWayTCP':len(success),'fatalOpenAtOriginalSite':sum(x['fatalOpen'] for x in rows),'successfulDockerCreateToStartMs':timing,'medianMs':statistics.median(timing) if timing else None}
rec['timingScope']='Docker create to start for successful guest attempts; failed starts have no start event. Two alternating pairs, one AMD64 worker.'
flush()
assert len(rec['trials'])==4 and rec['results']['configured']['twoWayTCP']==2 and rec['results']['configured']['fatalOpenAtOriginalSite']==0
assert len(rec['results']['configured']['successfulDockerCreateToStartMs'])==2
rec['qualification']='PASS';flush()
