"""Exercise the two commands actually selected by each Buildkite pipeline."""
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import qualification_report as qr

tmp = Path(os.environ['RUNNER_TEMP'])
out = tmp/'package-build-result'
out.mkdir(exist_ok=True)
now = lambda: dt.datetime.now(dt.timezone.utc).isoformat()
def git(*args): return subprocess.check_output(['git',*args])
def write(p, x): p.write_text(json.dumps(x,indent=2)+'\n')
source=os.environ['PRODUCT']
assert git('rev-parse','HEAD').decode().strip()==source
expected = ['bazel build //pkg/...','bazel build --build_tag_filters=-network_plugins //runsc/...']
for short,arches in (('first-pass',['AMD64']),('pipeline',['AMD64','ARM64'])):
    d=json.loads(subprocess.check_output(['ruby','-ryaml','-rjson','-e','puts JSON.generate(YAML.safe_load_file(ARGV.fetch(0), aliases: true))','.buildkite/'+short+'.yaml']))
    rows=[r for r in d['steps'] if isinstance(r,dict) and 'Build runsc and pkg' in r.get('label','')]
    assert len(rows)==len(arches)
    for arch,r in zip(arches,rows,strict=True):
        assert r['label'].endswith('('+arch+')') and r['env']['DOCKER_BUILD']=='false'
        assert r['commands']==expected

# Bazel authenticates through a private rc file. Only reconciled build events
# and reviewed error lines are exported; neither raw logs nor the rc leave Linux.
key=os.environ.pop('BUILDBUDDY_API_KEY')
if not key: raise ValueError('missing execution credential')
private=tmp/'native-package-private.bazelrc'
private.touch(mode=0o600,exist_ok=False)
platform=tmp/'package-platform'
platform.mkdir()
(platform/'REPO.bazel').write_text('')
image='docker://mirror.gcr.io/library/ubuntu@sha256:78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d'
(platform/'BUILD.bazel').write_text('platform(name="exec",visibility=["//visibility:public"],constraint_values=["@platforms//os:linux","@platforms//cpu:x86_64"],exec_properties='+str({'OSFamily':'linux','Arch':'amd64','container-image':image,'workload-isolation-type':'firecracker','EstimatedMemory':'4GB'})+')\n')
private.write_text(''.join('build --'+opt+'\n' for opt in ['remote_executor=grpcs://remote.buildbuddy.io','remote_cache=grpcs://remote.buildbuddy.io','bes_backend=grpcs://remote.buildbuddy.io','bes_results_url=https://app.buildbuddy.io/invocation/','spawn_strategy=remote','noremote_local_fallback','jobs=64','remote_download_outputs=minimal','inject_repository=package_exec='+str(platform),'extra_execution_platforms=@package_exec//:exec','host_platform=@package_exec//:exec','remote_header=x-buildbuddy-api-key='+key,'bes_header=x-buildbuddy-api-key='+key]))
summary={'source':source,'worker_image':image,'expected':['amd64-runsc','arm64-runsc'],'builds':{}}
def save(): write(out/'summary.json',summary)
save()
try:
    for arch,config in [('amd64','x86_64'),('arm64','aarch64')]:
        for scope,line in [('runsc',expected[1])]:
            name=arch+'-'+scope
            row=out/name
            row.mkdir()
            raw=tmp/(name+'.bep')
            log=tmp/(name+'.log')
            args=shlex.split(line)[1:]
            args[1:1]=['--config='+config,'--build_event_json_file='+str(raw)]
            files={k:k for k in ('source_before','source_after','commit_file','final_head','executed_arguments','native_exit','capture_exit')}
            (row/'source_before').write_bytes(git('status','--porcelain','--untracked-files=no'))
            (row/'commit_file').write_bytes(git('cat-file','commit','HEAD'))
            (row/'executed_arguments').write_text('\n'.join(args)+'\n')
            print('Starting '+name,flush=True)
            with log.open('wb') as stream:
                rc=subprocess.run(['bazel','--bazelrc='+str(private),*args],stdout=stream,stderr=subprocess.STDOUT).returncode
            finished=now()
            (row/'native_exit').write_text(str(rc))
            (row/'final_head').write_bytes(git('rev-parse','HEAD'))
            (row/'source_after').write_bytes(git('status','--porcelain','--untracked-files=no'))
            errors=[]
            invocation=None
            for text in log.open(errors='replace'):
                text=text.strip()
                if 'https://app.buildbuddy.io/invocation/' in text:
                    invocation=text.partition('https://app.buildbuddy.io/invocation/')[2][:36]
                if key in text or 'remote_header' in text or 'bes_header' in text or 'x-buildbuddy-api-key' in text.lower(): continue
                if len(errors)<50 and any(t in text for t in ('ERROR:','Error:','Error in ','error:','No such file','not found','not available','failed to execute','execvp','cannot execute','denied','undefined reference')):
                    errors.append(text[:1400])
            (row/'diagnostic.txt').write_text('\n'.join(errors)+'\n')
            retained=qr.capture(raw,row/'events',Path.cwd(),row/'reports')
            (row/'capture_exit').write_text(str(int(bool(retained['errors']))))
            write(row/'context.json',dict(files,command_kind='build',expected_source=source,execution_finished_at=finished))
            verdict=qr.verdict(row/'events',qr.manifest_path(row/'events'),row/'context.json')
            write(row/'verdict.json',verdict)
            completed=[]
            with (row/'events').open() as events:
                for event in map(json.loads,events):
                    if 'completed' in event:
                        completed.append([event['id']['targetCompleted'],event['completed'].get('success',False)])
            write(row/'completed-targets.json',completed)
            summary['builds'][name]=dict(status=verdict['status'],exit=rc,invocation=invocation,expected=len(verdict['expected']),completed=len(completed),succeeded=sum(ok for _,ok in completed),failures=len(verdict['failures']),missing=verdict['missing'],skips=verdict['skipped_owners'],execution_finished_at=finished,verdict_available_at=verdict['verdict_available_at'])
            save()
            print(name,verdict['status'],rc,len(verdict['expected']),'configured',flush=True)
finally:
    private.unlink(missing_ok=True)
    # Guard the complete export before upload, including unexpected errors
    # from tools. Never place client configuration or original logs in out.
    for f in out.rglob('*'):
        if f.is_file() and key.encode() in f.read_bytes():
            f.unlink()
            raise RuntimeError('removed evidence containing an execution credential')
summary['status']='PASS' if all(summary['builds'].get(n,{}).get('status')=='PASS' for n in summary['expected']) else 'FAIL'
summary['verdict_available_at']=now()
save()
sys.exit(int(summary['status']!='PASS'))
