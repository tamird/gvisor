"""Qualify only the six owners whose original dependency build failed."""

import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

import check as h


SOURCE = '778375bc602d883faf89a2f5b7bdf4016804da10'
CARRIER = '674c9bf24453910174c718b2fcc4365bbab248c6'
EXPORT = '1899f71d79be9b78ed439313ee050ff6b8d62a5d'
FIXTURE = 'tools/go_stateify/qualification_identtype'
LIB = '//' + FIXTURE + ':qualification_identtype'
TARGETS = ('//pkg/sentry/platform/kvm:kvm_state_autogen', '//pkg/sentry/platform/ptrace:ptrace_state_autogen',
           '//pkg/sentry/platform/systrap:systrap_state_autogen', '//runsc/boot/filter:filter_state_autogen',
           '//runsc/gofer/fs/filter:filter_state_autogen')
REPORT = h.ROOT / 'stateify-native-review.json'
OUT = dict(source=SOURCE, started=h.utc(), verdict='INCOMPLETE', phases={})


def save():
    REPORT.write_text(json.dumps(OUT,sort_keys=True,indent=2)+'\n')


def load(name, artifact, digest):
    data=(h.ROOT/name).read_bytes()
    assert h.digest(data)==digest
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        n,=z.namelist();assert n=='stateify-native-review.json'
        r=json.loads(z.read(n))
    OUT.setdefault('retained',{})[str(artifact)]=dict(zip_sha256=digest,verdict=r['verdict'])
    return r


def phase(name,fn):
    row=OUT['phases'][name]=dict(status='INCOMPLETE',started=h.utc())
    save()
    try:
        fn(row);row['status']='PASS'
    except Exception as exc:
        row['error_class']=type(exc).__name__;row['status']='FAIL'
    finally:
        row['finished']=h.utc();save()


def original(row):
    first=load('final-original.zip',13255863288,'6fea03c7b4adfccecd0fd4ad524fefcaa4ff58897ab5c90a48f7c6d88b96436e')
    diag=load('final-diagnostic.zip',13256054232,'db7dd9bf80290fa6a06808dd9981f7c0a3012df63e0d5b670353f09c8b8462d2')
    old=load('cached-producers.zip',13260765532,'4ae7d4ea29d686cf1449b8a373029c57d0ebe2b2b0e3d977e9859a03829717d9')
    assert old['source']==SOURCE and old['verdict']=='FAIL'
    failed=old['phases']['six_owners']
    assert failed['exit']==1 and not any(failed['completed'].values())
    cached=[a for a in old['phases']['stdlib_provenance']['actions'] if a['mnemonic']=='GoStdlib']
    assert len(cached)==6 and all(a['cacheHit'] for a in cached)
    assert sum(bool(o['excludedArchives']) for a in cached for o in a['outputs'])==2
    row.update(previous_native_exit=1,previous_cache_hits=6,previous_contaminated_sdk_trees=2)
    assert first['source']==diag['product']==SOURCE and first['verdict']=='FAIL' and diag['verdict']=='ORIGINAL_RECORDS_READ'
    assert first['phases']['exported_tree_480']['exact']==480 and first['phases']['seccomp_exported_inputs_only']['exact']==5
    assert first['phases']['in_tree_sibling_hook']['load_hook_xml_cases']==16
    assert set(first['phases']['five_with_seccomp_inputs']['missing'])==set(TARGETS)
    for name in ('candidate_imports','five_with_seccomp_inputs'):
        src=diag['invocations'][name]
        assert src['original_exit']==first['phases'][name]['exit']==1 and src['invocation']==first['phases'][name]['invocation']
        assert any('//tools/nogo:stdlib' in v['owners'] and 'FAILED:' in v['classes'] for v in src['console']['forward']['lines'])
    actions=first['phases']['candidate_imports']['generator_actions']
    old=first['phases']['retained']['stock_outputs']
    same=[k for k in old if '/qualification_' not in k or any(s in k for s in ('qualification_declared','qualification_unaliased'))]
    assert len(same)==9 and all(actions[k]['digest']==old[k] for k in same)
    assert not first['phases']['candidate_imports']['completed'][LIB] and actions[LIB+'_state_autogen']['exit']==0
    assert h.git('rev-parse','HEAD').decode().strip()==SOURCE and not h.git('diff','--name-only')
    shutil.copytree(Path(os.environ['CARRIER_DIR'])/FIXTURE,Path(FIXTURE))
    hashes={str(p):h.digest(p.read_bytes()) for p in Path(FIXTURE).iterdir() if p.is_file()}
    assert len(hashes)==2 and all(first['phases']['retained']['fixture_hashes'][k]==v for k,v in hashes.items())
    row.update(unchanged_stock=9,fresh_direct_stateify=19,real_hook_cases=16,real_exports=480,
               exported_seccomp_inputs=5,previous_generator_sha256=actions[LIB+'_state_autogen']['digest'])


def build(row):
    assert h.git('rev-parse','HEAD').decode().strip()==SOURCE and not h.git('diff','--name-only')
    bep=h.ROOT/'six.bep';compact=h.ROOT/'six.zst'
    identifier=str(uuid.uuid4())
    labels=[LIB,*TARGETS]
    native=['bazel','--bazelrc='+os.environ['QA_RC'],'build','--config=x86_64','--jobs=12','--keep_going',
            '--extra_execution_platforms=//tools/bazeldefs:linux_amd64','--spawn_strategy=remote','--noremote_local_fallback',
            '--remote_default_exec_properties=Arch=amd64','--remote_default_exec_properties=OSFamily=linux',
            '--remote_default_exec_properties=recycle-runner=false','--remote_default_exec_properties=workload-isolation-type=firecracker',
            '--remote_default_exec_properties=container-image='+h.GNU_IMAGE,
            '--strategy=GoStdlib=processwrapper-sandbox','--strategy=GoStdlibList=processwrapper-sandbox',
            # Bazel 8 uses only the last --modify_execution_info flag.
            '--modify_execution_info=GoStdlib(List)?=+no-remote-cache,GoStandardLibraryAnalysis=+no-cache',
            '--remote_download_outputs=toplevel','--remote_download_regex=.*_state_autogen.*[.]go$',
            '--build_event_json_file='+str(bep),'--nobuild_event_json_file_path_conversion',
            '--execution_log_compact_file='+str(compact),'--invocation_id='+identifier,*labels]
    row.update(invocation=identifier,expected=labels);save()
    p=subprocess.run(native,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    text=p.stdout.decode(errors='replace')
    row.update(exit=p.returncode,execution_finished=h.utc(),log_sha256=h.digest(p.stdout))
    row['original_failure_classes']={s:s in text for s in ('internal/testpty','present in stdlib GOROOT but not in source',
        'error filtering packages','OutOfMemoryError','No space left on device','NO_USABLE_STRATEGY_FOUND',
        'missing strict dependencies','could not import','undefined:', 'error running subcommand')}
    save()
    if compact.is_file():compact.chmod(0o644)
    events=list(h.rows(bep))
    row['completed']={e['id']['targetCompleted']['label']:e['completed'].get('success',False) for e in events
                      if 'targetCompleted' in e.get('id',{}) and e['id']['targetCompleted'].get('label') in labels}
    row['failed_actions']=[]
    for e in events:
        aid=e.get('id',{}).get('actionCompleted',{}); a=e.get('action',{})
        if not aid or not a.get('exitCode'):continue
        label=aid.get('label',''); pkg=label.split(':',1)[0]
        rec=dict(owner=label if re.fullmatch(r'//[A-Za-z0-9_/]+:[A-Za-z0-9_.+-]+',label) else 'external-or-unknown',
                 configuration=aid.get('configuration',{}).get('id'),exit=a['exitCode'])
        for k in ('stdout','stderr'):
            uri=a.get(k,{}).get('uri','')
            m=re.fullmatch(r'bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)',uri)
            if m:rec[k]=dict(sha256=m[1],bytes=int(m[2]))
        row['failed_actions'].append(rec)
    save()
    files={}
    for owner in TARGETS:
        pkg,name=owner[2:].split(':')
        path=pkg+'/'+name+'.go'
        ref=h.digest(h.git('show',EXPORT+':'+path))
        cur=Path('bazel-bin')/path
        files[owner]=dict(reference=ref,generated=h.digest(cur.read_bytes()) if row['completed'].get(owner) and cur.is_file() else None)
    row['files']=files
    row['library_completed']=row['completed'].get(LIB,False)
    save()
    assert p.returncode==0 and set(row['completed'])==set(labels) and all(row['completed'].values())
    assert all(f['generated']==f['reference'] for f in files.values())


def provenance(row):
    from stdlib_provenance import summarize
    p=h.ROOT/'six.zst'
    assert p.stat().st_size<128*1024**2
    evidence=summarize(p,False)
    assert evidence['invocation']==OUT['phases']['six_owners']['invocation'] and 0<len(evidence['actions'])<=64
    row['actions']=evidence['actions']
    producers=[a for a in row['actions'] if a['mnemonic']=='GoStdlib']
    analyses=[a for a in row['actions'] if a['mnemonic']=='GoStandardLibraryAnalysis']
    assert len(producers)==6 and analyses
    exact=[a for a in analyses if a['label']=='//tools/nogo:stdlib']
    assert exact and all(not a['cacheHit'] and a['runner'] and a['exit']==0 and not a['status'] for a in exact)
    assert {arch for a in exact for arch in a['architecture']}=={'-GOARCH=amd64','-GOARCH=arm64'}
    assert all(a['runner']=='processwrapper-sandbox' and not a['cacheHit'] and a['exit']==0 and not a['status'] for a in producers)
    assert all(a['outputs'] and all(o['archiveCount']>0 and not o['excludedArchives'] for o in a['outputs']) for a in producers)
    assert all(a['exit']==0 and not a['status'] for a in analyses)


if sys.argv[1:]==['provenance']:
    OUT=json.loads(REPORT.read_bytes())
    if 'six_owners' in OUT.get('phases',{}):phase('stdlib_provenance',provenance)
    OUT['verdict']='PASS' if set(OUT.get('phases',{}))=={'original','six_owners','stdlib_provenance'} and all(
        r['status']=='PASS' for r in OUT['phases'].values()) else 'FAIL'
    OUT['finished']=h.utc();save()
    sys.exit(OUT['verdict']!='PASS')
phase('original',original)
if OUT['phases']['original']['status']=='PASS':phase('six_owners',build)
sys.exit(not OUT['phases'].get('six_owners',{}).get('status')=='PASS')
