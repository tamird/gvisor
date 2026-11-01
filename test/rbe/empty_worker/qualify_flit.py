import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone
from zipfile import ZipFile

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "flit-evidence"
out.mkdir(exist_ok=True)
source = os.environ["PROOF"]
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
package = "//sandboxexec/sandbox/python"
results = dict(carrier=os.environ['GITHUB_SHA'], source=source, product=os.environ['PRODUCT'], worker=image,
               frontend="https://github.com/tamird/gvisor/actions/runs/41183200050/artifacts/13035474364", arms={})
def now():
    return datetime.now(timezone.utc).isoformat()
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def save():
    (out/"results.json").write_text(json.dumps(results,indent=2)+"\n")
original = tmp/"frontend.zip"
assert digest(original)=="4bb2c64b208e62937e40c7cfbc65b5a6fadd011e131f0713f054c7c8e8f93a2a"
with ZipFile(original) as z:
    expected=json.loads(z.read("test.outputs/backend-results.json"))['flit']
assert expected['status']=='PASS' and expected['pair_identical']
assert expected['build']==expected['uv'] and all(x['python']=='3.7.17' for x in expected['installs'].values())
results['baseline']={k:v['sha256'] for k,v in expected['build'].items()}
subprocess.run(["git","checkout","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
for arch,config in [('amd64','x86_64'),('arm64','aarch64')]:
    arm=results['arms'][arch]={'started':now()}
    try:
        platform='//test/rbe/empty_worker:'+arch
        runtime='//test/rbe/protoc_check:static_python'+('_arm64' if arch=='arm64' else '')
        log=tmp/(arch+'.binpb');bep=tmp/(arch+'.bep')
        a=['bazel','--bazelrc='+str(tmp/'sidecar.bazelrc'),'build','--config='+config,'--lockfile_mode=off',
           '--host_platform='+platform,'--extra_execution_platforms='+platform,'--extra_toolchains='+runtime,
           '--remote_download_outputs=toplevel','--noremote_local_fallback','--spawn_strategy=remote',
           '--modify_execution_info=^SandboxExecDist$=+no-remote-cache','--execution_log_compact_file='+str(log),
           '--build_event_json_file='+str(bep),'--nobuild_event_json_file_path_conversion','--'+package+':dist_version=2026.10.29.2',package+':dist']
        arm['args']=a[2:]
        p=subprocess.run(['timeout','--signal=INT','--kill-after=20s','15m',*a],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        arm['native_exit']=p.returncode
        arm['invocations']=re.findall(r'Invocation ID: ([0-9a-f-]{36})',p.stdout)
        lines=p.stdout.splitlines();err=[]
        for i,l in enumerate(lines):
            if 'ERROR:' in l or 'Traceback' in l:
                block='\n'.join(lines[i:i+18])
                if not re.search(r'api.?key|authorization|credential|remote_header',block,re.I):err.append(block[:3500])
        arm['errors']=err[:10]
        if log.is_file():
            with subprocess.Popen([str(tmp/'bb'),'print','--compact_execution_log='+str(log)],stdout=subprocess.PIPE) as dec:
                raw=subprocess.check_output(['jq','-c','select(.mnemonic=="SandboxExecDist") | {label:.targetLabel,mnemonic,argv:.commandArgs,runner,cacheHit,status,exitCode,platform,outputs:.actualOutputs}'],stdin=dec.stdout)
                dec.stdout.close();assert dec.wait()==0
            arm['actions']=[json.loads(r) for r in raw.splitlines()]
        assert p.returncode==0, 'Bazel returned '+str(p.returncode)
        action,=arm['actions']
        assert action['runner']=='remote' and not action['cacheHit'] and not action['exitCode'],action
        assert image in str(action['platform']) and 'python3.11' in action['argv'][0],action
        root=Path('bazel-bin/sandboxexec/sandbox/python/dist')
        arm['files']={f.name:digest(f) for f in root.iterdir()}
        arm['verdict']='MATCH' if arm['files']==results['baseline'] else 'DIFFERENT'
        if arm['verdict']=='DIFFERENT':
            import tarfile
            for f in sorted(root.iterdir()):
                vals={}
                if f.name.endswith('.whl'):
                    with ZipFile(f) as z:
                        for n in z.namelist():vals[n]=hashlib.sha256(z.read(n)).hexdigest()
                else:
                    with tarfile.open(f) as t:
                        for m in t:
                            if m.isfile(): vals[m.name]={'mtime':m.mtime,'sha256':hashlib.sha256(t.extractfile(m).read()).hexdigest()}
                (out/(arch+'-'+f.name+'.json')).write_text(json.dumps(vals,indent=2))
    except Exception as exc:
        arm['verdict']='INCOMPLETE';arm['reason']=repr(exc)[:700]
    arm['finished']=now();save();print(arch,arm.get('native_exit'),arm['verdict'],arm.get('errors'),arm.get('reason'),flush=True)
results['verified']=now();save()
assert all(x['verdict']=='MATCH' for x in results['arms'].values()),results['arms'].keys()
