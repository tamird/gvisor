"""Record the real release archive writer and its first failing remote action."""
from pathlib import Path, PurePosixPath
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import traceback

import exec_cas as cas

SOURCE = 'bbbed5b2b84fe21597c740789d841e618f912def'
TARGET = '//debian:gvisor-release-tar-bz2'
TARGETS = {TARGET, '//debian:gvisor-bin-tar'}
ANALYSIS = '5a604e81-2ddd-479d-bd48-e28d19cb4060'
INV = '164843ea-c23e-4a9d-ac4e-1de51ec4f18f'
TMP = Path(os.environ['RUNNER_TEMP']) / 'release-rbe'
REPORT = TMP / 'public-tar.json'
PROPS = {'Arch':'arm64', 'OSFamily':'linux', 'workload-isolation-type':'oci', 'recycle-runner':'false',
         'container-image':'docker://gcr.io/distroless/static-debian13@sha256:b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428'}
MEMBERS = {'runsc', 'containerd-shim-runsc-v1'} | {'gvisor-bin/' + s for s in (
    'checkpointgofer', 'gvisor_sentry', 'gvisor-sentry-prewarmer', 'runsc-fd-parking',
    'runsc-metric-server', 'runsc-netgofer', 'gvisor-proc-umounter')}


def save(row):
    REPORT.write_text(json.dumps(row,indent=2)+'\n')


def safe(v):
    return v if isinstance(v,str) and re.fullmatch(r'[@A-Za-z0-9_+:./-]{1,380}',v) else '<unavailable>'


def digest_of(ref):
    if not isinstance(ref,dict) or not re.fullmatch('[0-9a-f]{64}',ref.get('hash','')):return None
    n=int(ref.get('sizeBytes',0))
    return {'hash':ref['hash'],'sizeBytes':n} if n >= 0 else None


def stderr_ref(f):
    if not isinstance(f,dict):return None
    m=re.fullmatch(r'bytestream://(?:remote\.buildbuddy\.io|bytestream\.buildbuddy\.io)/[^?#]*?blobs/([0-9a-f]{64})/(\d+)',f.get('uri',''))
    return {'hash':m[1], 'sizeBytes':int(m[2])} if m else None


def classify(raw):
    low=raw.lower()
    return {s.decode():True for s in (b'no such file or directory',b'exec format error',b'permission denied',
        b'failed to start',b'fork/exec',b'build_tar',b'python3',b'/usr/bin/env',b'/bin/bash',b'/bin/sh',
        b'native startup requires',b'no native python launcher',b'no matching toolchain',b'no matching execution',
        b'checksum', b'failed to fetch', b'no such package', b'no repository visible', b'no module named',
        b'libpython',b'error while loading shared libraries',b'no space left',b'resource exhausted',b'oom') if s in low}


def command(*args):return subprocess.check_output(args,text=True).strip()


def executable_kind(raw):
    if raw.startswith(b'\x7fELF'):
        return {'elf':cas.native(raw)}
    if raw.startswith(b'#!'):
        line=raw.split(b'\n',1)[0].rstrip()
        matched=re.fullmatch(rb'#!\s*(?:(?:/usr/bin/env\s+)?(?:/usr/bin/|/bin/)?(?:ba|da)?sh|(?:/usr/bin/env\s+)?(?:/usr/bin/|/bin/)?python[0-9.]*)',line)
        return {'shebang':line.decode() if matched else '<other interpreter>'}
    return {'format':'other'}


def run(row):
    assert command('git','rev-parse','HEAD')==SOURCE and command('git','rev-parse','HEAD:debian/BUILD')=='00412de320be320c9cbee06463906dec55bac125'
    assert command('git','rev-parse','HEAD:MODULE.bazel')=='05d430dccc0c028d423189a1a699849057bc4c52'
    assert command('git','diff','--name-only','HEAD')=='' and command('git','ls-files','--others','--exclude-standard')=='test/rbe/release_prewarmer/BUILD'
    assert hashlib.sha256(Path('test/rbe/release_prewarmer/BUILD').read_bytes()).hexdigest()=='ae1f23a11f3cb747fbec49a34c510c5ab9dcb75d06d94bfd4d9a49d50c629271'
    bazel=[str(TMP/'bazel'),'--nosystem_rc','--nohome_rc','--bazelrc='+str(TMP/'private.bazelrc')]
    flags=['--config=x86_64','-c','opt','--platforms=@llvm//platforms:linux_x86_64_glibc_static','--extra_execution_platforms=//test/rbe/release_prewarmer:arm64',
        '--host_platform=//test/rbe/release_prewarmer:arm64','--extra_toolchains=//test/rbe/go_bootstrap:linux_arm64_ash',
         '--extra_toolchains=//test/rbe/release_python:arm64_musl',
        '--@rules_python//python/config_settings:bootstrap_impl=native',
        '--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain' ,'--jobs=40','--lockfile_mode=off',
        '--remote_download_outputs=toplevel',r'--remote_download_regex=.*/(pkg/private/tar/build_tar|debian/(gvisor-bin-tar|gvisor-release-tar-bz2)\.manifest)$' ,'--modify_execution_info=^PackageTar$=+no-remote-cache','--color=no','--curses=no','--noshow_progress']
    graph=subprocess.run([*bazel,'aquery',*flags,'--invocation_id='+ANALYSIS,'--noinclude_artifacts','--include_param_files','--output=jsonproto',
         'mnemonic("PackageTar", deps('+TARGET+'))'],capture_output=True,timeout=420)
    row['analysis']={'invocation':ANALYSIS,'exit':graph.returncode,'stderr_sha256':hashlib.sha256(graph.stderr).hexdigest(),'diagnostic':classify(graph.stderr)}
    save(row)
    assert graph.returncode==0
    g=json.loads(graph.stdout); owners={str(t['id']):t['label'].removeprefix('@@') for t in g['targets']}
    writers={owners[str(a['targetId'])]:safe(a['arguments'][0]) for a in g['actions'] if a['mnemonic']=='PackageTar'}
    row['writers']={k:writers[k] for k in TARGETS if k in writers}
    row['coordinator']=os.uname().machine
    assert row['coordinator']=='x86_64'
    row['target_platform']='@llvm//platforms:linux_x86_64_glibc_static'
    contracts={}
    for a in g['actions']:
        owner=owners[str(a['targetId'])]
        if a['mnemonic']!='PackageTar' or owner not in TARGETS:continue
        args=a['arguments']
        for pf in a.get('paramFiles', []):args=args+pf.get('arguments',[])
        contract={}
        for flag in ('mode','manifest','directory'):
            flagname='--'+flag
            value=args[args.index(flagname)+1] if flagname in args else next((x.split('=',1)[1] for x in args if x.startswith(flagname+'=')),None)
            if value is not None:contract[flag]=safe(value)
        contracts[owner]=contract
    row['writer_contracts']=contracts
    row['configuration']={'compiler_base':'d5fe3c2eef21434cbe13e3604f0f070ace4d7edb','rules_python':'804470ef813ec3a25df2432d374f84bac6ec2d76',
        'interpreter_archive_sha256':'976cfd9b50e2fd37c21b74816f4372abc4db21ebaef4bdf3e1690dcb9d1c66c1',
        'launcher':'hermetic_launcher@0.0.17'}
    save(row)
    assert set(row['writers'])==TARGETS
    bep=TMP/'archive.bep'; compact=TMP/'archive.compact'
    with (TMP/'build.log').open('wb') as log:
        row['exit']=subprocess.run(['timeout','--kill-after=20','2500',*bazel,'build',*flags,'--invocation_id='+INV,
            '--build_event_publish_all_actions','--build_event_json_file='+str(bep), '--execution_log_compact_file='+str(compact),TARGET,
            '//runsc', '//shim:containerd-shim-runsc-v1', '//runsc/cmd/sentry:gvisor_sentry',
            '//runsc/checkpointgofer:checkpointgofer_binary','//runsc/cmd/metricserver:runsc-metric-server',
            '//runsc/cmd/netgofer:runsc-netgofer','//runsc/fdparking:fdparking_binary',
            '//runsc/prewarmer:prewarmer_binary','//runsc/procumounter:procumounter_binary'],
            stdout=log,stderr=subprocess.STDOUT).returncode
    first=None
    for l in bep.open():
        event=json.loads(l); key=event['id']
        if 'started' in event:assert event['started']['uuid']==INV
        if 'finished' in event:row['bazel_exit']=event['finished']['exitCode'].get('code',0)
        if key.get('targetCompleted',{}).get('label')==TARGET and 'completed' in event and not key['targetCompleted'].get('aspect'):
            row['completed']=event['completed'].get('success',False)
        if a:=event.get('action'):
            k=key['actionCompleted'];owner=safe(k.get('label'));mnemonic=safe(a.get('type'))
            if not a.get('success') and first is None:
                first=row['first_failure']={'owner':owner,'mnemonic':mnemonic,'output':safe(k.get('primaryOutput')),
                    'exit':a.get('exitCode'), 'stderr':stderr_ref(a.get('stderr')), 'stdout':stderr_ref(a.get('stdout')),
                    'diagnostic':classify(str(a.get('failureDetail',{}).get('message','')).encode()),
                    'failure_code':safe(a.get('failureDetail',{}).get('spawn',{}).get('code'))}
    row['build_log']={'sha256':hashlib.sha256((TMP/'build.log').read_bytes()).hexdigest(),'diagnostic':classify((TMP/'build.log').read_bytes())}
    save(row)
    query='''select(.spawn) | .spawn | select((.mnemonic=="PackageTar" and (.targetLabel|startswith("//debian:"))) or ((.exitCode//0)!=0)) |
          [.targetLabel,.mnemonic,.args[0],.runner,(.cacheHit//false),(.exitCode//0),
          ([.platform.properties[]?|{key:.name,value:.value}]|from_entries|contains($p))]'''
    with subprocess.Popen([TMP/'bb','print','--raw','--compact_execution_log='+str(compact)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as rd:
        r=subprocess.run(['jq','-c','--argjson','p',json.dumps(PROPS),query],stdin=rd.stdout,capture_output=True);rd.stdout.close()
        assert r.returncode==rd.wait()==0
    row['executions']=[]
    for s in r.stdout.splitlines()[:16]:
        owner,mnemonic,exe,runner,cached,code,matched=json.loads(s)
        row['executions'].append({'owner':safe(owner),'mnemonic':safe(mnemonic),'executable':safe(exe),'runner':safe(runner),'cached':cached,'exit':code,'worker_matches':matched})
    for owner,exe in row['writers'].items():
        p=Path(exe)
        if not p.is_absolute() and p.parts[0]=='bazel-out' and '..' not in p.parts and p.is_file() and p.stat().st_size<=48*1024*1024:
            if owner not in {s['owner'] for s in row['executions'] if s['executable']==exe}:continue
            raw=p.read_bytes()
            row.setdefault('downloaded_executables',{})[owner]={'sha256':hashlib.sha256(raw).hexdigest(),**executable_kind(raw)}
    save(row)
    if row['exit']!=0:
        assert row['bazel_exit']!=0 and first
        row['status']='BUILD_FAILED'
        return
    assert row['bazel_exit']==0 and row['completed'] and first is None
    assert {s['owner'] for s in row['executions'] if s['mnemonic']=='PackageTar' and s['runner']=='remote' and not s['cached'] and s['worker_matches'] and s['exit']==0}==TARGETS
    archive=Path('bazel-bin/debian/gvisor.tar.bz2');row['archive_size']=archive.stat().st_size;row['archive_digest']=hashlib.sha256(archive.read_bytes()).hexdigest();files={}
    with tarfile.open(archive,'r:bz2') as tf:
        for m in tf:
            name=m.name.removeprefix('./').rstrip('/')
            if m.isdir():assert name in ('','.','gvisor-bin');continue
            assert name in MEMBERS and name not in files and m.isfile() and 0<m.size<250*1024**2 and m.mode&0o7777==0o755
            raw=tf.extractfile(m).read(); assert raw[:6]==b'\x7fELF\x02\x01' and int.from_bytes(raw[18:20],'little')==62
            files[name]={'bytes':m.size,'mode':m.mode,'sha256':hashlib.sha256(raw).hexdigest()}
    assert set(files)==MEMBERS
    row['files']=files
    save(row)
    inputs={}
    for owner,contract in contracts.items():
        mode=int(contract['mode'],8)
        path=Path(contract['manifest'])
        assert mode==0o755 and path.parts[0]=='bazel-out' and '..' not in path.parts and path.suffix=='.manifest' and path.stat().st_size<32768
        entries=json.loads(path.read_bytes())
        assert len(entries)==(2 if owner==TARGET else 7)
        prefix=contract.get('directory','/').strip('/')
        assert prefix==('' if owner==TARGET else 'gvisor-bin')
        for record in entries:
            assert record['type']=='file'
            name='/'.join(x for x in (prefix,record['dest'].lstrip('/')) if x)
            assert name in files and name not in inputs
            src=Path(record['src'])
            assert src.parts[0]=='bazel-out' and '..' not in src.parts and 0<src.stat().st_size<250*1024**2
            effective=int(record['mode'],8) if record['mode'] else mode
            disk_mode=stat.S_IMODE(src.stat().st_mode)
            with src.open('rb') as f:h=hashlib.file_digest(f,'sha256').hexdigest() if hasattr(hashlib,'file_digest') else hashlib.sha256(f.read()).hexdigest()
            inputs[name]={'owner':owner,'source':safe(str(src)),'sha256':h,'declared_mode':effective,'materialized_mode':disk_mode}
            assert h==files[name]['sha256'] and effective==files[name]['mode'] and src.stat().st_size==files[name]['bytes'] and disk_mode&0o111
    assert set(inputs)==MEMBERS
    row['input_files']=inputs;save(row)
    assert set(row['downloaded_executables'])==TARGETS and all(v['elf']['machine']==183 and v['elf']['interpreter'] is None for v in row['downloaded_executables'].values())
    row['status']='PASS'


def cas_lookup(row):
    failure=row.get('first_failure')
    if not failure:return
    assert row['invocation']==INV==cas.INV
    name,mnemonic=failure['owner'],failure['mnemonic']
    assert safe(name)==name and safe(mnemonic)==mnemonic and '<' not in name+mnemonic
    evidence=row['original_execution']={}
    # Restrict the lookup to the first failing owner and the fixed original run.
    matches=cas.executions(name,mnemonic)
    for e in matches[:12]:
        response=e.get('executeResponse',{});output=response.get('result',{})
        if output.get('exitCode',0)==0 and response.get('status',{}).get('code',0)==0:continue
        if not digest_of(e.get('actionDigest')):continue
        addr=evidence['action']=digest_of(e['actionDigest']);evidence['exit']=output.get('exitCode',0);evidence['rpc_code']=response.get('status',{}).get('code',0)
        err=base64.b64decode(output.get('stderrRaw',''))
        errref=digest_of(output.get('stderrDigest')) or failure.get('stderr')
        if errref:evidence['stderr']=errref
        if not err and errref and 0<errref['sizeBytes']<=512*1024:err=cas.blob(errref,512*1024)
        evidence['stderr_class']=classify(err+str(response.get('status',{}).get('message','')).encode())
        evidence['stderr_content_sha256']=hashlib.sha256(err).hexdigest()
        save(row)
        ac=dict(cas.fields(cas.blob(addr,128*1024)));cr=cas.digest_msg(ac[1]);evidence['command']=cr
        exe=next(v.decode() for k,v in cas.fields(cas.blob(cr,4*1024*1024)) if k==1)
        evidence['executable']=safe(exe); root=cas.digest_msg(ac[2]);evidence['input_root']=root
        save(row)
        if name not in TARGETS or mnemonic!='PackageTar':return
        assert exe==row['writers'][name] and safe(exe)==exe
        if exe.startswith('/'):return
        parts=PurePosixPath(exe).parts
        assert '..' not in parts and 0<len(parts)<=30
        for i,part in enumerate(parts):
            last=i+1==len(parts);directory=cas.blob(root,4*1024*1024)
            node,=[item for k,v in cas.fields(directory) if k==(1 if last else 2) and (item:=dict(cas.fields(v))).get(1)==part.encode()]
            root=cas.digest_msg(node[2])
        evidence['executable_blob']=root;save(row)
        raw=cas.blob(root,48*1024*1024)
        evidence.update(executable_kind(raw))
        return


def report_error(row,exc):
    return {'type':type(exc).__name__,'line':traceback.extract_tb(exc.__traceback__)[-1].lineno}


mode,=sys.argv[1:]
if mode=='build':
    row={'source':SOURCE,'target':TARGET,'invocation':INV,'status':'INCOMPLETE','run':os.environ['GITHUB_RUN_ID'],'worker':PROPS}
    try: run(row)
    except Exception as exc:row['report_error']=report_error(row,exc)
    finally:save(row)
elif mode=='diagnose':
    row=json.loads(REPORT.read_bytes())
    assert row['source']==SOURCE and row['target']==TARGET and row['invocation']==INV and row['run']==os.environ['GITHUB_RUN_ID']
    try:cas_lookup(row)
    except Exception as exc:row['diagnostic_error']=report_error(row,exc)
    finally:save(row)
else: raise ValueError('unknown mode')
