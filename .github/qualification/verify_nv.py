"""Run the project update commands, verify the full catalog, and exercise the two NVIDIA targets."""
from pathlib import Path
import hashlib,json,os,re,subprocess,shutil,time,xml.etree.ElementTree as ET
root=Path(os.environ['RUNNER_TEMP'])/'llvm-source-presubmit'
results=root/'results'
source=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
env=dict(os.environ, PATH=str(root)+':'+os.environ['PATH'], MAKEFLAGS='DOCKER_BUILD=false')
pkg='//pkg/sentry/devices/nvproxy:'
abi,checksums=pkg+'nvproxy_driver_abi_test',pkg+'nvproxy_driver_checksums_test'
driver='580.196.07'
missing_git,missing_pin='executable file not found in $PATH','is missing from the source index'
def bazel(leg,subcmd,args,opts):
    cmd=[str(root/'bazel'),'--host_jvm_args=-Xmx10g','--nosystem_rc','--nohome_rc','--bazelrc='+str(root/'remote.rc'),subcmd,*opts,*args]
    with (root/(leg+'.log')).open('w') as f: result=subprocess.run(cmd,env=env,stdout=f,stderr=subprocess.STDOUT)
    log=(root/(leg+'.log')).read_text(errors='replace')
    notes=[line.split(' (kept existing entry')[0][:600] for line in log.splitlines() if ('all ' in line and 'dependencies have up-to-date' in line) or ('nvidia_driver_580_196_07' in line and ' ' in line) or '580.196.07:' in line or line.startswith(('ERROR:','licensecheck:','FATAL:'))]
    safe=[re.sub(r'https?://[^ ]+[?][^ ]+','[download URL]',l) for l in notes if not re.search(r'authorization:|bearer |api-key|remote_header|bes_header|github_pat_',l,re.I)]
    print(leg,result.returncode,safe[:8],flush=True)
    (results/(leg+'.json')).write_text(json.dumps({'source':source,'exit':result.returncode,'notes':safe[:30]},indent=2))
    return result.returncode,log
def git(*args):return subprocess.check_output(['git',*args])
def store(p,name):
    data=p.read_bytes();(results/name).write_bytes(data);return hashlib.sha256(data).hexdigest()
def run(variant,src,labels,pattern,arch):
    cpu={'amd64':'x86_64','arm64':'aarch64'}[arch]
    platform='//tools/bazeldefs:linux_'+arch
    arm=variant+'-'+arch
    bep=root/(arm+'.bep')
    log=root/(arm+'.log')
    command=[str(root/'bazel'),'--host_jvm_args=-Xmx10g','--nosystem_rc','--nohome_rc','--bazelrc='+str(root/'remote.rc'),'test','--config='+cpu,'--host_platform='+platform,'--extra_execution_platforms='+platform,'--remote_default_exec_properties=Arch='+arch,'--distdir='+str(root/'dist'),'--keep_going','--jobs=40','--strip=never','--test_env=GO_TEST_WRAP_TESTV=1','--nocache_test_results','--flaky_test_attempts=1','--test_output=errors','--remote_download_regex=.*test[.](xml|log)$','--build_event_json_file='+str(bep)]
    if pattern:
        command+=['--test_filter='+pattern]
    with log.open('w') as stream:
        status=subprocess.run([*command,'--',*labels],stdout=stream,stderr=subprocess.STDOUT).returncode
    receipts={}
    inv=''
    if bep.is_file():
        for line in bep.open():
            event=json.loads(line)
            if 'started' in event:
                inv=event['started'].get('uuid','')
            key=event.get('id',{}).get('testSummary')
            if not key:continue
            name=key['label']
            suite=Path('bazel-testlogs')/name[2:].replace(':','/')
            row={'status':event.get('testSummary',{}).get('overallStatus'),'cases':[], 'diagnostics':[]}
            text=''
            for ext in ('log','xml'):
                p=suite/('test.'+ext)
                if not p.is_file():continue
                raw=p.read_bytes()
                row[ext+'_sha256']=hashlib.sha256(raw).hexdigest()
                if ext=='log':
                    text=raw.decode(errors='replace')
                    for ln in text.splitlines():
                        clean=re.sub(r'[\x00-\x1f]','',ln).strip()
                        if any(w in clean for w in (missing_git,missing_pin,'failed to run driver_ast_parser','checksum mismatch','certificate signed','--- FAIL:','--- SKIP:','driver source is not available')):
                            row['diagnostics'].append(clean[:540])
                else:
                    xml=ET.fromstring(raw)
                    for item in xml.iter('testcase'):
                        result='skipped' if item.find('skipped') is not None else 'failed' if any(item.find(s) is not None for s in ('failure','error')) else 'passed'
                        row['cases'].append({'name':item.get('name'),'status':result})
            row['version_outcomes']={m.group(2):m.group(1) for m in re.finditer(r'--- (PASS|SKIP|FAIL): (?:TestStructDefinitionParity|TestDriverChecksums)/(\d+\.\d+\.\d+)(?=\s)',text)}
            receipts[name]=row
    data={'variant':variant,'source':src,'arch':arch,'status':status,'invocation':inv,'receipts':receipts}
    if not receipts or status not in (0,3):
        lines=log.read_text(errors='replace').splitlines()
        data['build_errors']=[l[:1100] for l in lines if l.startswith(('ERROR:','FATAL:','WARNING: Download from'))][:50]
    (results/(arm+'.json')).write_text(json.dumps(data,indent=2)+'\n')
    if variant!='updated-index':
        why=missing_git if variant=='baseline' else missing_pin
        ok=len(labels)==len(receipts)==1 and status!=0 and any(why in d for d in receipts[labels[0]]['diagnostics'])
    else:
        ok=status==0 and set(receipts)==set(labels)
        for label in labels:
            row=receipts.get(label,{})
            outcomes=row.get('version_outcomes',{})
            print(arm,label,row.get('status'),{keyword:list(outcomes.values()).count(keyword) for keyword in ('PASS','SKIP','FAIL')},outcomes.get(driver),flush=True)
            ok=ok and row.get('status')=='PASSED' and len(outcomes)==35 and outcomes.get(driver)=='PASS'
            ok=ok and list(outcomes.values()).count('PASS')==(26 if label==abi else 35)
    print(arm,status,inv,'expected result observed:',ok,flush=True)
    return ok


pin=Path('tools/nvidia_driver_differ/driver_pins.bzl')
mod=Path('MODULE.bazel');catalog=Path('tools/licensecheck/dependencies.yaml')
opts=['--config=x86_64','--jobs=40','--distdir='+str(root/'dist')]
original_pin=pin.read_bytes();original_mod=mod.read_bytes();original_catalog=catalog.read_bytes()
report={'source':source,'checks':{}}
def note(k,v):
    report['checks'][k]=v
    (results/'generation-receipt.json').write_text(json.dumps(report,indent=2)+'\n')
notice='nvidia_driver_580_196_07'
assert 'bazel_dep(name = "llvm", version = "0.8.26")' in mod.read_text()
up,_=bazel('source-updater','run',['//tools/nvidia_driver_differ/sources:update'],opts)
generated_pins=pin.read_bytes()
note('source_updater',{'exit':up,'matched':generated_pins==original_pin,'sha256':store(pin,'driver_pins.bzl')})
tidy,_=bazel('mod-tidy','mod',['tidy','--distdir='+str(root/'dist')],[])
tidy_matched=mod.read_bytes()==original_mod
note('mod_tidy',{'exit':tidy,'matched':tidy_matched,'sha256':store(mod,'MODULE.bazel')})
generated_diff=git('diff','--','MODULE.bazel','tools/nvidia_driver_differ/driver_pins.bzl')
(results/'generated-inputs.diff').write_bytes(generated_diff)
# Use the signed proposal when checking its licensing policy and running the binaries.
mod.write_bytes(original_mod);pin.write_bytes(original_pin)
license_target='//tools/licensecheck/main:licensecheck'
check,log=bazel('verify-full-catalog','run',[license_target,'--','--mode=verify'],opts)
note('verify',{'exit':check,'summary':re.findall(r'all \d+ dependencies have up-to-date license entries',log)})
s=original_catalog.decode()
block=re.search(r'(?m)^- dependency: '+notice+r'\n(?:  [^\n]*\n)+',s)
assert block is not None
scratch=root/'catalog.yaml';scratch.write_text(s[:block.start()]+s[block.end():])
no,log=bazel('verify-omitted','run',[license_target,'--','--yaml='+str(scratch),'--mode=verify'],opts)
named=any(notice in l and ('missing' in l or 'no license' in l) for l in log.splitlines())
note('negative',{'exit':no,'names_missing_entry':named})
fetch,log=bazel('fetch-omitted','run',[license_target,'--','--yaml='+str(scratch),'--mode=fetch'],opts)
s2=scratch.read_text(); regenerated=re.search(r'(?m)^- dependency: '+notice+r'\n(?:  [^\n]*\n)+',s2)
matched=regenerated is not None and regenerated.group()==block.group()
(results/'nvidia-driver-catalog-entry.yaml').write_text(regenerated.group() if regenerated else '')
note('regenerated_license',{'exit':fetch,'matched_entry':matched,'sha256':hashlib.sha256(scratch.read_bytes()).hexdigest(),'full_catalog_identical':scratch.read_bytes()==original_catalog})
checks_ok=up==0 and generated_pins==original_pin and tidy==0 and tidy_matched and check==0 and no!=0 and named and fetch==0 and matched
for arch in ('amd64','arm64'):
    checks_ok=run('updated-index',source,[abi,checksums],'',arch) and checks_ok
raise SystemExit(0 if checks_ok else 1)
