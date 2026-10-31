"""Reuse the Bazel-built objdump; collect one package action's real input digests."""
import hashlib,json,os,re,subprocess as sp,traceback
from pathlib import Path
from collections import Counter,defaultdict

tmp=Path(os.environ['RUNNER_TEMP']); rec=tmp/'records'
docker=['docker','exec','tcp-compiler']
bazel=docker+['bazel','--bazelrc='+str(tmp/'tcp.bazelrc')]
def save(name,v): (rec/name).write_text(json.dumps(v,indent=2)+'\n')
def parse(f):
    dec=json.JSONDecoder()
    with f.open() as file:
        buf=''
        while True:
            chunk=file.read(131072);buf+=chunk
            while (buf:=buf.lstrip()):
                try:v,pos=dec.raw_decode(buf)
                except ValueError:
                    if not chunk: raise
                    break
                yield v
                buf=buf[pos:]
            if not chunk: return
def result_inputs(spawn):
    files={}
    for f in spawn.get('inputs',[]):
        n=f['path'];assert n not in files,n;files[n]=f
    digest={}
    part=defaultdict(Counter)
    for n,x in files.items():
        dig=x.get('digest',{});size=int(dig.get('sizeBytes',0));h=dig.get('hash')
        if h:
            if h in digest: assert digest[h]==size,h
            digest[h]=size
        g=('target standard library' if '/stdlib_/' in n or n.endswith('/stdlib_') else
           'SDK sources' if '/src/' in n and n.startswith('external/') else
           'SDK tools' if '/pkg/tool/' in n or n.endswith('/bin/go') else
           'SDK headers' if '/pkg/include/' in n else
           'materialized objdump' if n.endswith('/nogo_materialized/objdump') else 'other')
        part[g].update({'paths':1,'bytes':size,'missing_digest':not bool(h)})
    return files,{'paths':len(files),'paths_with_digest':sum(bool(x.get('digest',{}).get('hash')) for x in files.values()),'bytes_by_path':sum(int(x.get('digest',{}).get('sizeBytes',0)) for x in files.values()),'unique_digests':len(digest),'bytes_by_unique_digest':sum(digest.values()),'groups':dict(part)}
def run(mode,strategy):
    full=tmp/(mode+'.spawns.json');raw=tmp/(mode+'.log')
    args=['test','--config=x86_64','--noremote_upload_local_results','--incompatible_modify_execution_info_additive',
         '--strategy=GoStdlib=linux-sandbox','--strategy=GoStdlibList=linux-sandbox',
         '--modify_execution_info=^GoStaticAnalysis$=+no-remote-cache,^GoStdlib(List)?$=+no-remote-cache',
         '--remote_download_outputs=toplevel','--jobs=1','--strategy=GoStaticAnalysis='+strategy,
         '--strategy=GoStandardLibraryAnalysis=local','--strategy=TestRunner=local','--nocache_test_results',
         '--test_output=all','--define=nogo_probe='+mode,'--execution_log_json_file='+str(full),'//tools/nogo/sanity:sanity_test']
    with raw.open('w') as out:
        rc=sp.run(bazel+args,stdout=out,stderr=sp.STDOUT).returncode
    log=raw.read_text(errors='replace')
    (rec/(mode+'-diagnostics.txt')).write_text('\n'.join(l for l in log.splitlines()
        if any(k in l for k in ['NOGO-','ERROR:','FAILED','error','Invocation ID:','format %d','PASS','error running','no such file','Error in','FAIL:']))+'\n')
    if rc: raise ValueError((mode,rc,log[-1500:]))
    assert 'fmt.Fprintf format %d has arg "hello world!" of wrong type string' in log,mode
    traces=re.findall(r'NOGO-OBJDUMP mode='+mode+r' duration_ms=(\d+)',log)
    assert len(traces)==2,(mode,traces,log[-1000:])
    rows=[]
    for a in parse(full):
        if a.get('targetLabel')!='//tools/nogo/sanity:sanity' or 'check' not in a.get('commandArgs',[]):continue
        assert a.get('runner')==strategy and not a.get('cacheHit',False),a.get('runner')
        outs=a.get('actualOutputs',[]);arch=a['listedOutputs'][0].split('/')[1].split('-')[0]
        files,stats=result_inputs(a)
        assert stats['paths']>5 and stats['paths_with_digest'] and stats['bytes_by_path']>1000,stats
        save(mode+'-'+arch+'-inputs.json',list(files.values()))
        rows.append({'arch':arch,'runner':a.get('runner'),'cacheHit':a.get('cacheHit',False),'metrics':a['metrics'],'inputs':stats,'outputs':outs})
    assert len(rows)==2,(mode,len(rows))
    record={'mode':mode,'uncached_spawns':rows,'objdump_process_ms':list(map(int,traces)),'expected_finding':True}
    if mode=='warm':
        cachepaths=set(re.findall(r'NOGO-CACHED-OBJDUMP (\S+)',log))
        assert cachepaths and all('/.checkescape-gocache/' in x and x.startswith('/') for x in cachepaths),cachepaths
        digests={sp.check_output(docker+['sha256sum',n],text=True).split()[0] for n in cachepaths}
        assert len(digests)==1,digests
        target='test/rbe/nogo_materialized/objdump'
        assert Path(target).read_text().startswith('Replaced on the Linux runner')
        sp.run(docker+['cp','--',sorted(cachepaths)[0],target],check=True)
        assert Path(target).read_bytes()[:4]==b'\x7fELF' and hashlib.sha256(Path(target).read_bytes()).hexdigest() in digests
        record['original_sdk_tool_sha256']=digests.pop()
    return record
rows=[]
try:
    source=sp.check_output(['git','rev-parse','HEAD'],text=True).strip()
    assert source==os.environ['CURRENT'],source
    for mode,strategy in [('warm','local'),('local','local'),('sandbox','linux-sandbox'),('direct','linux-sandbox'),('lean','linux-sandbox')]:
        rows.append(run(mode,strategy)); save('measurements.json',rows)
    gs={r['mode']:r['uncached_spawns'][0]['inputs']['groups'] for r in rows}
    assert gs['local']['SDK sources']['paths']>500 and gs['sandbox']['SDK sources']['paths']>500,gs
    assert gs['direct']['SDK sources']['paths']>500 and 'SDK sources' not in gs['lean'],gs
    save('verdict.json',{'status':'PASS','source':source,'scopes':[r['mode'] for r in rows],'consumer':'required native printf finding on both architecture configurations'})
except BaseException:
    save('verdict.json',{'status':'FAIL','complete_scopes':[r['mode'] for r in rows],'trace':traceback.format_exc()})
    raise
