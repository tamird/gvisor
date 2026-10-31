import hashlib,json,os,re,subprocess as sp,traceback
from pathlib import Path
tmp=Path(os.environ['RUNNER_TEMP']); rec=tmp/'records'
spec=json.loads((tmp/'sdk-spec.json').read_text());errs={}
def save(name,data): (rec/name).write_text(json.dumps(data,indent=2)+'\n')
def check(cmd,**kw):return sp.check_output(cmd,text=True,**kw).strip()
def remember(name,fn):
    try: return fn()
    except BaseException:
        errs[name]=traceback.format_exc();save(name+'-failure.json',errs[name]);return None
def execute(name,cmd,**kw):
    log=tmp/(name+'.log')
    with log.open('w') as out: rc=sp.run(cmd,stdout=out,stderr=sp.STDOUT,**kw).returncode
    tail=log.read_text(errors='replace')
    diag=[l for l in tail.splitlines() if any(s in l for s in ('Invocation ID:', 'PASS','FAIL','ERROR:','error:','error running','error loading','Error in','not found','no such file','Exception','Build completed','processes:','%d has arg'))]
    (rec/(name+'-diagnostics.txt')).write_text('\n'.join(diag[-90:]+tail.splitlines()[-20:])+'\n')
    if rc: raise RuntimeError((name,rc,'\n'.join(diag[-10:])))
    return tail
def parse(path):
    dec=json.JSONDecoder()
    with path.open() as f:
        buf=''
        while True:
            chunk=f.read(131072);buf+=chunk
            while (buf:=buf.lstrip()):
                try:step,i=dec.raw_decode(buf)
                except ValueError:
                    if not chunk:raise
                    break
                yield step
                buf=buf[i:]
            if not chunk: return
def supplier():
    repo=tmp/'rules_go';repo.mkdir()
    sp.run(['git','init','-q',str(repo)],check=True)
    sp.run(['git','-C',str(repo),'fetch','--quiet','--depth=1','https://github.com/bazel-contrib/rules_go.git',spec['supplierBase']],check=True)
    sp.run(['git','-C',str(repo),'checkout','--quiet','--detach','FETCH_HEAD'],check=True)
    sp.run(['git','-C',str(repo),'apply','--index',str(tmp/'sdk-supplier.patch')],check=True)
    assert check(['git','-C',str(repo),'write-tree'])==spec['supplierTree']
    version=(repo/'.bazelversion').read_text().strip()
    env=dict(os.environ,USE_BAZEL_VERSION=version)
    exe=check(['which','bazelisk']) if sp.run(['which','bazelisk'],stdout=sp.DEVNULL).returncode==0 else check(['which','bazel'])
    assert check([exe,'--version'],cwd=repo,env=env).endswith(version),version
    log=tmp/'supplier.spawns.json'
    args=[exe,'test','--test_output=all','--nocache_test_results','--test_arg=-test.v','--jobs=3','--execution_log_json_file='+str(log),'//tests/core/stdlib:buildid_test']
    out=execute('supplier',args,cwd=repo,env=env)
    assert '--- PASS: TestEmptyBuildID' in out,out[-1000:]
    acts=[a for a in parse(log) if a.get('mnemonic')=='GoSDKTool']
    assert any(a.get('runner') and 'buildid' in a.get('commandArgs',[]) and a.get('exitCode',0)==0 for a in acts),[(a['targetLabel'],a['commandArgs']) for a in acts]
    save('supplier.json',{'source':spec['supplier'],'tree':spec['supplierTree'],'bazel':version,'test':'TestEmptyBuildID','actions':[{k:a[k] for k in ('targetLabel','runner','exitCode','commandArgs','actualOutputs') if k in a} for a in acts]})
def gvisor():
    current=check(['git','rev-parse','HEAD']);assert current==os.environ['CURRENT']
    assert check(['git','rev-parse','HEAD^'])==spec['consumer']
    assert check(['git','rev-parse','HEAD^^'])==spec['gvisorBase']
    files=check(['git','diff','--name-only','HEAD^','HEAD']).splitlines()
    assert files==['MODULE.bazel','tools/rules_go_sdk_tool_qualification.patch'],files
    docker=['docker','exec','tcp-compiler'];bazel=docker+['bazel','--bazelrc='+str(tmp/'tcp.bazelrc')]
    log=tmp/'gvisor.spawns.json'
    args=['test','--config=x86_64','--noremote_upload_local_results','--incompatible_modify_execution_info_additive','--jobs=3',
          '--strategy=GoStdlib=linux-sandbox','--strategy=GoStdlibList=linux-sandbox','--strategy=GoSDKTool=linux-sandbox','--strategy=GoStaticAnalysis=linux-sandbox','--strategy=GoStandardLibraryAnalysis=linux-sandbox','--strategy=TestRunner=local',
          '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(StaticAnalysis|StandardLibraryAnalysis|SDKTool|Stdlib(List)?)$=+no-remote-cache',
          '--nocache_test_results','--test_output=all','--test_arg=-test.v','--remote_download_outputs=toplevel','--execution_log_json_file='+str(log),
          '//tools/nogo/sanity:sanity_test','//pkg/goid:goid_test','//pkg/goid:goid_asm_render']
    content=execute('gvisor',bazel+args);assert 'fmt.Fprintf format %d has arg "hello world!" of wrong type string' in content and '--- PASS: TestUniquenessAndConsistency' in content,content[-1200:]
    known=spec['outputs'];rows=[];sdk=[];seen={};digests=set()
    owners={'bundle':'//tools/nogo:stdlib','check':'//tools/nogo/sanity:sanity','filter':'//tools/nogo/sanity:sanity_nogo','render':'//pkg/goid:goid_asm_render'}
    for a in parse(log):
        m=a.get('mnemonic');argv=a.get('commandArgs',[]);label=a.get('targetLabel');outputs=a.get('actualOutputs',[])
        if m=='GoSDKTool' and label=='//tools/nogo:objdump':
            assert a.get('runner')=='linux-sandbox' and not a.get('cacheHit') and a.get('exitCode',0)==0,a
            sdk.append({k:a.get(k) for k in ('mnemonic','targetLabel','runner','cacheHit','commandArgs','metrics','actualOutputs')});digests.update(f['digest']['hash'] for f in outputs)
        roles=[k for k,v in owners.items() if label==v and k in argv]
        if not roles: continue
        role,=roles
        assert a.get('runner')=='linux-sandbox' and not a.get('cacheHit') and a.get('exitCode',0)==0,(role,a.get('runner'))
        objs=[x.split('=',1)[1] for x in argv if x.startswith('-objdump=')]
        if role in ('check','bundle'):
            assert len(objs)==1 and any(x['path']==objs[0] and x.get('digest',{}).get('hash') for x in a['inputs']),(role,objs)
        else:assert not objs,(role,objs)
        if role=='check':assert not any(x['path'].startswith('external/') and ('/src/' in x['path'] or x['path'].endswith('/bin/go')) for x in a.get('inputs',[]))
        for f in outputs:
            n=f['path'];key=n.split('/')[1].split('-')[0]+'/'+n.split('/')[-1];seen[key]=f['digest']['hash']
        rows.append({'role':role,'owner':label,'runner':a['runner'],'cacheHit':a.get('cacheHit',False),'actualOutputs':outputs,'time':a['metrics'],'inputs':len(a.get('inputs',[])),'objdump':[(x['path'],x['digest']['hash']) for x in a.get('inputs',[]) if x['path'] in objs]})
    assert sdk and len(digests)==1,(sdk,digests)
    assert all(x[1] in digests for row in rows for x in row['objdump']),digests
    assert {v['role'] for v in rows}==set(owners)
    missing={k:(v,seen.get(k)) for k,v in known.items() if v!=seen.get(k)}
    save('gvisor-output-comparison.json',{'source':spec['consumer'],'qualified_tree':current,'original':known,'actual':seen,'different':missing,'note':'The stock stdlib raw finding GOBs changed between two unchanged runs; those bytes are reported separately.'})
    save('production-analyzer-actions.json',rows);save('go-sdk-objdump-producer.json',sdk)
    assert not missing,missing
    save('gvisor.json',{'source':spec['consumer'],'rules_go_v0.63_patch':spec['pinned'],'sanity_finding':True,'goid_test':'TestUniquenessAndConsistency','matched_stable_outputs':len(known),'producer_digests':sorted(digests)})
remember('gvisor',gvisor)
remember('supplier',supplier)
save('verdict.json',{'status':'FAIL' if errs else 'PASS','sources':spec,'errors':errs})
raise SystemExit(bool(errs))
