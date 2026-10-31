from pathlib import Path
import json,os,re,subprocess,sys
d=Path(os.environ['RUNNER_TEMP']);out=d/'arena-posture';out.mkdir(exist_ok=True)
rec={'products':{},'status':'RUNNING'};expect={'pure','cgo','plugin','missing','unexpected_pure','old_rseq_only','malformed','extra_tunable','extra_key','literal_placeholder'};good=True
def record(): (out/'verdict.json').write_text(json.dumps(rec,indent=2)+'\n')
def run(arm,op,flags,labels):
 dest=d/(arm+'-'+op);bep=dest.with_suffix('.bep');log=dest.with_suffix('.log')
 cmd=['bazelisk','--bazelrc='+str(d/'images.bazelrc'),op,'--config=x86_64','--color=no','--curses=no','--build_event_json_file='+str(bep),*flags,*labels]
 with log.open('w') as f:q=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT)
 r={'exit':q.returncode,'targets':{}}
 if bep.exists():
  for ln in bep.open():
   j=json.loads(ln);id=j.get('id',{})
   if 'started' in j:r['invocation']=j['started']['uuid']
   if 'targetCompleted' in id and (n:=id['targetCompleted']['label']) in labels:r['targets'][n]=j.get('completed',{}).get('success',False)
   if 'testSummary' in j:r['overall']=j['testSummary']['overallStatus']
   if 'testResult' in j:
    v=j['testResult'];r.setdefault('tests',[]).append({'status':v['status'],'cached':v.get('cachedLocally',False) or v.get('executionInfo',{}).get('cachedRemotely',False),'runner':v.get('executionInfo',{}).get('strategy','')})
 if q.returncode:r['diagnostics']=[ln[:780].strip() for ln in log.open(errors='replace') if re.match(r'(?:ERROR:|Resource Exhausted:|FAILED:)',ln) and not re.search(r'header|credential|bearer|key|authorization',ln,re.I)][:10]
 return r
for arm in ['original','fixed']:
 prod=os.environ[arm.upper()];fix=os.environ[arm.upper()+'_FIXTURE'];r=rec['products'][arm]={'product':prod,'fixture':fix};record()
 subprocess.run(['git','checkout','--detach',fix],check=True)
 changed=subprocess.check_output(['git','diff','--name-only',prod,fix],text=True).splitlines()
 assert changed==['pkg/test/sandboxposture/BUILD','pkg/test/sandboxposture/arena_probe_test.go'],changed
 lab='//pkg/test/sandboxposture:arena_probe_test'
 r['test']=run(arm,'test',['--strategy=TestRunner=linux-sandbox','--nocache_test_results','--test_sharding_strategy=disabled','--test_filter=^TestArenaPosture$','--test_env=GO_TEST_WRAP_TESTV=1','--test_arg=-test.v','--test_output=errors'],[lab]);t=r['test']
 p=Path('bazel-testlogs/pkg/test/sandboxposture/arena_probe_test/test.log')
 s=p.read_text(errors='replace') if p.exists() else ''
 (out/(arm+'.test.log')).write_text('\n'.join(line for line in s.splitlines() if re.search(r'(?:=== RUN|--- (?:PASS|FAIL|SKIP):|collected |fatal error:)',line))+'\n')
 statuses={n:v for v,n in re.findall(r'--- (PASS|FAIL|SKIP): TestArenaPosture/(\w+)',s)};r['cases']=statuses
 failures={k for k,v in statuses.items() if v!='PASS'}
 wanted={'cgo','plugin','old_rseq_only'} if arm=='original' else set()
 good=good and set(statuses)==expect and failures==wanted and t.get('overall')==('FAILED' if wanted else 'PASSED') and len(t.get('tests',[]))==1 and not t['tests'][0]['cached'] and t['tests'][0]['runner']=='linux-sandbox'
 record()
subprocess.run(['git','checkout','--detach',os.environ['FIXED']],check=True)
owners=['//test/root:root_test','//runsc/cmd:cmd_test']
r=rec['ownerBuild']=run('fixed','build',['--keep_going'],owners);good=good and r['exit']==0 and r['targets']==dict.fromkeys(owners,True)
rec['status']='PASS' if good else 'FAIL';record();sys.exit(not good)
