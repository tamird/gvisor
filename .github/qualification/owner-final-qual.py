"""Verify only the final boot_nogo owner; retain the original emitted records."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root=Path(os.environ['RUNNER_TEMP'])
out=root/'exact-checkpoint-nogo'
out.mkdir(exist_ok=True)
arch=sys.argv[1]
assert arch in ('amd64','arm64')
target='//runsc/boot:boot_nogo'
src=os.environ['SOURCE']
rec={'source':src,'arch':arch,'target':target,'result':'FAIL','test_summaries':[], 'actions':[]}
def verify():
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==src
    bep=root/(arch+'-exact-owner.bep')
    log=root/(arch+'-exact-owner.log')
    docker=['docker','exec','-e','GIT_CONFIG_COUNT=1','-e','GIT_CONFIG_KEY_0=safe.directory','-e','GIT_CONFIG_VALUE_0='+os.environ['GITHUB_WORKSPACE'],'overlay-guest']
    argv=docker+['bazel','--bazelrc='+str(root/'overlay.bazelrc'),'test',
          '--config='+{'amd64':'x86_64','arm64':'aarch64'}[arch],'--platforms=//tools/bazeldefs:linux_'+arch,
          '-c','opt','--jobs=4','--strategy=GoStaticAnalysis=linux-sandbox','--strategy=GoStandardLibraryAnalysis=linux-sandbox',
          '--strategy=GoStdlib=linux-sandbox','--strategy=GoStdlibList=linux-sandbox',
          '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache',
          '--remote_download_outputs=toplevel','--stamp','--workspace_status_command=',
          '--build_event_json_file='+str(bep),'--build_event_publish_all_actions','--test_output=errors',target]
    with log.open('w') as f: rec['exit']=subprocess.run(argv,stdout=f,stderr=subprocess.STDOUT).returncode
    rec['invocations']=sorted(set(re.findall(r'https://app.buildbuddy.io/invocation/[0-9a-f-]+',log.read_text(errors='replace'))))
    complete=False;raw=[]
    if bep.is_file():
        with bep.open() as f:
            for ln in f:
                e=json.loads(ln)
                eid=e.get('id',{})
                for kind in ('targetCompleted','testResult','testSummary'):
                    if eid.get(kind,{}).get('label')==target:
                        raw.append(e)
                        if kind=='targetCompleted' and e.get('completed',{}).get('success'):complete=True
                        if kind=='testSummary':rec['test_summaries'].append(e['testSummary'].get('overallStatus'))
                a=e.get('action',{})
                output=eid.get('actionCompleted',{}).get('primaryOutput','')
                if a.get('type')=='GoStaticAnalysis' and ('/runsc/boot/' in output or '/runsc/version/' in output):
                    rec['actions'].append({'output':output,'succeeded':a.get('success',False),'exit':a.get('exitCode',0)})
    (out/(arch+'-owner-events.json')).write_text(json.dumps(raw,indent=2)+'\n')
    rec['target_completed']=complete
    if rec['exit'] or not complete or rec['test_summaries']!=['PASSED']:
        body=re.sub(r'(?i)(x-buildbuddy-api-key[=: ]+)\S+',r'\1[redacted]',log.read_text(errors='replace'))
        errors=[line for line in body.splitlines() if 'ERROR:' in line or 'Killed' in line]
        (out/(arch+'-diagnostics.txt')).write_text('\n'.join(errors[:12])+'\n'+body[-12000:])
        raise RuntimeError(f"{arch} {target}: exit {rec['exit']}, complete={complete}, summaries={rec['test_summaries']}")
    rec['result']='PASS'
try: verify()
finally:
    rec['recorded']=datetime.now(timezone.utc).isoformat()
    (out/(arch+'.json')).write_text(json.dumps(rec,indent=2)+'\n')
    print(json.dumps(rec),flush=True)
