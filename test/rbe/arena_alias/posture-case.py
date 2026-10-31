import hashlib,json,os,pathlib,subprocess,sys,time
P=pathlib.Path;d=P(os.environ['RUNNER_TEMP']);out=d/'receipt';out.mkdir(exist_ok=True)
def sha(p):return hashlib.file_digest(p.open('rb'),'sha256').hexdigest() if hasattr(hashlib,'file_digest') else hashlib.sha256(p.read_bytes()).hexdigest()
if sys.argv[1:]==['observe']:
 mode=os.environ['CASE_MODE'];p=d/('posture-'+mode)/'gvisor-bin/gvisor_sentry';inode=p.stat();records={};ready=d/(mode+'.ready');stop=d/(mode+'.stop');ready.touch()
 while not stop.exists():
  for q in P('/proc').iterdir():
   if not q.name.isdigit() or q.name in records:continue
   try:
    st=(q/'exe').stat()
    if (st.st_dev,st.st_ino)!=(inode.st_dev,inode.st_ino):continue
    bits=(q/'environ').read_bytes().split(b'\0')
    if not any(v.startswith(b'GODEBUG=') for v in bits):continue
    records[q.name]=[v.decode() for v in bits if v.startswith((b'GLIBC_TUNABLES=',b'MALLOC_ARENA_MAX=',b'glibc.malloc.arena_max='))]
   except OSError:continue
  time.sleep(.02)
 (out/(mode+'-process.json')).write_text(json.dumps({'matchedSentrySha256':sha(p),'perProcessTunables':list(records.values())},indent=2)+'\n')
 sys.exit(0)
import re
img='alpine@sha256:b276d875eeed9c7d3f1cfa7edb06b22ed22b14219a7d67c52c56612330348239'
subprocess.run(['docker','pull',img],check=True);subprocess.run(['docker','tag',img,'gvisor.dev/images/basic/alpine'],check=True)
pkg=P('test/rbe/tldk_posture_binary');pkg.mkdir(parents=True,exist_ok=True)
import shutil
shutil.copy2(d/'posture-test',pkg/'root_test')
(pkg/'BUILD').write_text('load("@bazel_skylib//rules:native_binary.bzl", "native_test")\nnative_test(name="posture",src="root_test",size="small",local=True)\n')
wrap=d/'sudo-posture.py';wrap.write_text("""#!/usr/bin/python3
import os,subprocess,sys
# Bazel's outer test wrapper is unprivileged, but its real root test is not.
r=subprocess.run(['sudo','--preserve-env=XML_OUTPUT_FILE,TEST_SRCDIR,TEST_WORKSPACE,TEST_TMPDIR','--',*sys.argv[1:]])
p=os.environ.get('XML_OUTPUT_FILE')
if p and os.path.isfile(p):
 c=subprocess.run(['sudo','chown',f'{os.getuid()}:{os.getgid()}',p])
 if c.returncode and not r.returncode:sys.exit(c.returncode)
sys.exit(r.returncode)
""");wrap.chmod(0o755)
r={'source':os.environ['PRODUCT'],'alpine':img,'rootTestSha256':sha(d/'posture-test'),'runs':{}}
for mode in ('pure','race'):
 runtime='qualification-posture-'+mode;target=d/('posture-'+mode)
 subprocess.run(['sudo',target/'runsc','install','--runtime='+runtime,'--download-sidecars=NEVER','--','--platform=systrap','--network=sandbox'],check=True)
subprocess.run(['sudo','systemctl','reload','docker'],check=True)
for mode in ('pure','race'):
 runtime='qualification-posture-'+mode;target=d/('posture-'+mode)
 obs=subprocess.Popen(['sudo','env','RUNNER_TEMP='+str(d),'CASE_MODE='+mode,'python3',__file__,'observe'])
 try:
  for _ in range(100):
   if (d/(mode+'.ready')).exists() or obs.poll()!=None:break
   time.sleep(.05)
  assert (d/(mode+'.ready')).exists()
  cmd=['bazelisk','test','--strategy=TestRunner=local','--spawn_strategy=local','--nocache_test_results','--noremote_accept_cached','--test_sharding_strategy=disabled','--test_output=all','--test_timeout=120','--run_under='+str(wrap),'--test_arg=-test.v','--test_arg=-test.run=^TestSandboxProcessEnv$','--test_arg=-runtime='+runtime,'--test_arg=-platform=systrap','--build_event_json_file='+str(d/(mode+'-test.bep')),'//test/rbe/tldk_posture_binary:posture']
  with (d/(mode+'-posture.log')).open('w') as f:cmdres=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT)
 finally:
  (d/(mode+'.stop')).touch();assert obs.wait(timeout=10)==0
 f=P('bazel-testlogs/test/rbe/tldk_posture_binary/posture/test.log');s=f.read_text(errors='replace') if f.exists() else ''
 row=r['runs'][mode]={'launcher':sha(target/'runsc'),'caseStarts':re.findall(r'^=== RUN\s+(\S+)',s,re.M),'casePasses':re.findall(r'^--- PASS:\s+(\S+)',s,re.M),'caseSkips':re.findall(r'^--- SKIP:\s+(\S+)',s,re.M),'exit':cmdres.returncode}
 for line in (d/(mode+'-test.bep')).read_text().splitlines():
  e=json.loads(line)
  if 'testResult' in e:q=e['testResult'];row.update(status=q.get('status'),cached=q.get('cachedLocally',False),execution=q.get('executionInfo',{}))
 # The fixture dumps the environment upon an unexpected variable: never copy it into this receipt.
 if row['exit']:row['diagnosticClasses']=[x for x in ['sandbox process\'s environment is not empty','requires sysadmin privileges','docker run failed','no test files','permission denied','environment is not empty','startup failed','unexpected sandbox process environment','SandboxPid','operation not permitted'] if x in s]
 (out/'root-results.json').write_text(json.dumps(r,indent=2)+'\n')
for mode,row in r['runs'].items():
 assert row['exit']==0 and row['status']=='PASSED' and row['caseStarts']==row['casePasses']==['TestSandboxProcessEnv'] and not row['caseSkips'] and not row['cached'] and row['execution']['strategy'] in ('standalone','local'),row
 envs=json.loads((out/(mode+'-process.json')).read_text())['perProcessTunables'];assert envs
 if mode=='pure':assert all(v==[] for v in envs),envs
 else:assert all(len(v)==2 and v[0]=='GLIBC_TUNABLES=glibc.pthread.rseq=0' and re.fullmatch(r'MALLOC_ARENA_MAX=[1-9][0-9]*',v[1]) and int(v[1].split('=')[1])%8==0 for v in envs),envs
r['status']='PASS';(out/'root-results.json').write_text(json.dumps(r,indent=2)+'\n')
