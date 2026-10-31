"""Build the actual runsc sidecars and verify their compiler/linker records on the empty worker."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess

ROOT = Path(os.environ['RUNNER_TEMP'])
OUT = ROOT / 'sidecar-evidence'
OUT.mkdir(exist_ok=True)
IMAGE = 'docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef'
PROGRAMS = {'fdparking': 'runsc-fd-parking', 'prewarmer': 'gvisor-sentry-prewarmer', 'procumounter': 'gvisor-proc-umounter'}
TARGETS = [f'//runsc/{p}:{p}_binary' for p in PROGRAMS]
def properties(row):
    return {p['name']:p['value'] for p in row.get('platform',{}).get('properties',[])}
def indexed_rows(path):
    if not path.exists():
        return []
    decoded = subprocess.run([str(ROOT/'bb'), 'print', '--compact_execution_log='+str(path)], stdout=subprocess.PIPE, check=True)
    jq = subprocess.run(['jq','-c','select((.targetLabel // "") | startswith("//runsc/") or startswith("@@//runsc/")) , select((.exitCode // 0) != 0 or (.status // "") != "") | {targetLabel,mnemonic,runner,cacheHit,exitCode,status,digest,platform,tool:.commandArgs[0],actualOutputs,listedOutputs}'], input=decoded.stdout,stdout=subprocess.PIPE,check=True)
    return [json.loads(line) for line in jq.stdout.splitlines()]
def run(arch,phase):
    config={'amd64':'x86_64','arm64':'aarch64'}[arch]
    folder=OUT/f'{arch}-{phase}';folder.mkdir()
    log=ROOT/f'{arch}-{phase}.binpb';bep=ROOT/f'{arch}-{phase}.jsonl';txt=ROOT/f'{arch}-{phase}.log'
    args=['bazel','--bazelrc='+str(ROOT/'sidecar.bazelrc'),'build','--config='+config,'--jobs=36','--remote_download_outputs=toplevel','--color=no','--curses=no','--host_platform=//test/rbe/native_sidecars:'+arch,'--extra_execution_platforms=//test/rbe/native_sidecars:'+arch,'--execution_log_compact_file='+str(log),'--build_event_json_file='+str(bep),'--nobuild_event_json_file_path_conversion',*TARGETS]
    with txt.open('w') as f:
        status=subprocess.run(args,stdout=f,stderr=subprocess.STDOUT).returncode
    text=txt.read_text(errors='replace')
    details=[s[:550] for s in text.splitlines() if 'api-key' not in s.lower() and re.search(r'ERROR:|error:|No such file|No such target|failed to start|exec format|unable to find library|Could not find|interpreter',s,re.I)]
    events=[json.loads(l) for l in bep.read_text().splitlines()] if bep.exists() else []
    record={'phase':phase,'arch':arch,'exit':status,'source':os.environ['GITHUB_SHA'],'target':TARGETS,'invocation':next((e['started']['uuid'] for e in events if 'started' in e),None),'errors':details[-16:]}
    print(arch,phase,'native Bazel exit',status,'https://app.buildbuddy.io/invocation/'+str(record['invocation']),flush=True)
    print('\n'.join(record['errors']),flush=True)
    try:
        rows=indexed_rows(log)
        record['failed_actions']=[{k:v for k,v in r.items() if k!='actualOutputs'} for r in rows if r.get('exitCode') or r.get('status')]
        if not status:
            results={}
            for pkg,name in PROGRAMS.items():
                file=Path('bazel-bin/runsc')/pkg/name;b=file.read_bytes()
                assert b[:6]==b'\x7fELF\x02\x01',file
                machine=struct.unpack_from('<H',b,18)[0];assert machine=={'amd64':62,'arm64':183}[arch]
                phoff=struct.unpack_from('<Q',b,32)[0];size,n=struct.unpack_from('<HH',b,54);types=[struct.unpack_from('<I',b,phoff+i*size)[0] for i in range(n)]
                assert 3 not in types and 2 not in types and (pkg=="fdparking" or len(b)<=4096),(name,types,len(b))
                digest=hashlib.sha256(b).hexdigest();ops={}
                for m in ('CppCompile','CppLink'):
                    match=[x for x in rows if x.get('mnemonic')==m and '/runsc/'+pkg+':' in x['targetLabel']]
                    assert len(match)==1,(pkg,m,[x['targetLabel'] for x in match])
                    x=match[0];props=properties(x)
                    assert x['runner']=='remote' and not x['cacheHit'] and not x['exitCode'] and props['Arch']==arch and props['container-image']==IMAGE,(m,name,x['runner'],x['cacheHit'],props)
                    if m=='CppLink':
                        assert any(o.get('digest',{}).get('hash')==digest for o in x['actualOutputs']),(m,name)
                    ops[m]={'tool':x['tool'],'digest':x['digest']['hash'],'cacheHit':False,'platform':props}
                results[name]={'bytes':len(b),'sha256':digest,'machine':machine,'elf_program_types':types,'actions':ops}
            record['ELFs']=results
        record['verified']=status==0
    except Exception as err:
        record['verified']=False
        record['verification_error']=str(err)[:600]
        print('Evidence verification:',record['verification_error'],flush=True)
    finally:
        (folder/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    return record
def override(names):
    p=Path('MODULE.bazel');s=original
    before='patches = ["//tools:llvm-linux-header-default.patch"],'
    assert s.count(before)==1
    p.write_text(s.replace(before,'patches = ['+', '.join('"//tools:'+x+'"' for x in ['llvm-linux-header-default.patch',*names])+'],'))
original=Path('MODULE.bazel').read_text()
results=[]
for arch in ['amd64','arm64']:
    Path('MODULE.bazel').write_text(original)
    x=run(arch,'registry');results.append(x)
    if not x['exit']:
        continue
    errors=' '.join(x['errors']).lower()
    if 'module-map-generator' in errors:
        override(['qualification-module-map-musl.patch'])
        x=run(arch,'module-map');results.append(x)
        patches=['qualification-module-map-musl.patch']
    else:
        patches=[]
    if x['exit'] and ('-lpthread' in ' '.join(x['errors']) or '-ldl' in ' '.join(x['errors'])):
        override([*patches,'qualification-linux-libraries.patch'])
        x=run(arch,'supplier-link');results.append(x)
Path('MODULE.bazel').write_text(original)
assert all(any(r.get('verified') and r['arch']==a for r in results) for a in ['amd64','arm64'])
