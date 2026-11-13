"""Compare the compiler integration, unchanged NVIDIA proposals, and one new source pin."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

root = Path(os.environ['RUNNER_TEMP']) / 'llvm-source-presubmit'
results = root / 'results'
baseline, proposed, current = (root / 'sources').read_text().splitlines()
pkg = '//pkg/sentry/devices/nvproxy:'
combined = pkg+'nvproxy_driver_parity_test'
abi = pkg+'nvproxy_driver_abi_test'
checksums = pkg+'nvproxy_driver_checksums_test'
driver = '580.196.07'
missing_pin = 'driver 580.196.07 is missing from the source index'
missing_git = 'executable file not found in $PATH'
cases = (
    ('baseline',baseline,[combined],'^TestStructDefinitionParity$/^580[.]196[.]07$'),
    ('original-index',proposed,[abi],'^TestStructDefinitionParity$/^580[.]196[.]07$'),
    ('updated-index',current,[abi,checksums],''),
)

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

good=True
for variant,src,labels,pattern in cases:
    subprocess.run(['git','switch','--detach',src],check=True)
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==src
    assert 'bazel_dep(name = "llvm", version = "0.8.26")' in Path('MODULE.bazel').read_text()
    for arch in ('amd64','arm64'):
        good=run(variant,src,labels,pattern,arch) and good
raise SystemExit(0 if good else 1)
