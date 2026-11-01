"""Inspect original wildcard outputs and the REAPI commands that linked them."""
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

tmp=Path(os.environ['RUNNER_TEMP'])
product='022894f6b57bac7ed55c741ea07a3611dc256da9'
key=os.environ['BUILDBUDDY_API_KEY']
exe=tmp/'bb'
out=tmp/'coverage-linked.json'
results=dict(source=product,wildcard='41252118749',status='INCOMPLETE',architectures={})
def save():
    out.write_text(json.dumps(results,indent=2)+'\n')
def safe(exc):
    return type(exc).__name__+(' '+str(exc.code) if hasattr(exc,'code') else (': '+str(exc)[:240] if isinstance(exc,(ValueError,KeyError,AssertionError)) else ''))
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None
op=urllib.request.build_opener(NoRedirect)
def read(url,data=None):
    assert url.startswith('https://app.buildbuddy.io/')
    req=urllib.request.Request(url,data,{'Content-Type':'application/json','x-buildbuddy-api-key':key})
    with op.open(req,timeout=65) as r:return r.read()
def cas(inv,dig):
    h,n=dig['hash'],int(dig['sizeBytes'])
    assert re.fullmatch('[0-9a-f]{64}',h) and 0<=n<150_000_000
    uri='bytestream://remote.buildbuddy.io/blobs/'+h+'/'+str(n)
    b=read('https://app.buildbuddy.io/file/download?'+urlencode({'invocation_id':inv,'bytestream_url':uri,'filename':h}))
    assert len(b)==n and hashlib.sha256(b).hexdigest()==h
    return b
def proto(kind,dig):
    args=[str(exe),'download','--target=grpcs://remote.buildbuddy.io','--api_key='+key,'--type='+kind,dig['hash']+'/'+str(dig['sizeBytes'])]
    p=subprocess.run(args,capture_output=True,timeout=50)
    if p.returncode:raise ValueError('CAS '+kind+' reader returned '+str(p.returncode))
    return json.loads(p.stdout)
def params(inv,root,path,cache):
    item=root
    parts=Path(path).parts
    assert not Path(path).is_absolute() and '..' not in parts
    for i,name in enumerate(parts):
        h=item['hash']
        if h not in cache:cache[h]=proto('Directory',item)
        directory=cache[h]
        nodes=directory.get('files' if i==len(parts)-1 else 'directories',[])
        item=next(x['digest'] for x in nodes if x['name']==name)
    return cas(inv,item).decode()
def read_link(inv,e):
    dg=e['actionDigest']; a=proto('Action',dg); c=proto('Command',a['commandDigest'])
    raw=c['arguments']; args=list(raw); cache={}
    for it in raw:
        filename=(it[7:] if it.startswith('-param=') else it[1:] if it.startswith('@') else '')
        if filename and (filename.endswith('.params') or 'params' in filename or it.startswith('-param=')):
            args.extend(params(inv,a['inputRootDigest'],filename,cache).splitlines())
    tokens=[t for s in args for t in re.split(r"[\s=\x00'\x22]+",s)]
    return dict(owner=e.get('targetLabel'),action=dg['hash'],command=c['arguments'][0],output=e.get('primaryOutputPath'),race='-race' in tokens,external=any(tokens[i:i+2]==['-linkmode','external'] for i in range(len(tokens)-1)),static='-static' in tokens,musl_args=[t[-240:] for t in tokens if 'musl' in t and ('sysroot' in t or 'clang' in t or 'platform' in t)][:4])
def uleb(b,at,end):
    v=0
    for i in range(5):
        if at>=end:raise ValueError('truncated table')
        c=b[at];at+=1;v|=(c&127)<<(7*i)
        if c<128:return v,at
    raise ValueError('overflow')
def covmeta(b):
    # internal/coverage.MetaSymbolHeader + per-function offsets + string table.
    # Go inserts the empty string, package path, then package name. A normal
    # qualified gVisor package has path/name indices 1/2. Require the full
    # header, offsets and string-table boundary before accepting any match.
    found={}
    for m in re.finditer(b'\x02\0\0\0\x01\0\0\0',b):
        at=m.start()-4
        if at<0 or at+44>=len(b):continue
        size,name,path,module,meta,pad,ns,nf=struct.unpack_from('<IIII16sIII',b,at)
        end=at+size
        if not(44<size<=len(b)-at and 0<nf<300_000 and 2<ns<100_000 and pad==0 and module<ns and 44+4*nf<size):continue
        st=at+44+4*nf
        first=at+struct.unpack_from('<I',b,at+44)[0]
        if not st<first<end:continue
        try:
            n,st=uleb(b,st,first)
            if n!=ns:continue
            strings=[]
            for _ in range(n):
                length,st=uleb(b,st,first)
                if st+length>first:raise ValueError('bad string')
                strings.append(b[st:st+length]);st+=length
            if st!=first or strings[0] or not strings[path].startswith(b'gvisor.dev/gvisor/'):continue
            offsets=struct.unpack_from('<'+'I'*nf,b,at+44)
            if offsets[-1]>=size or any(a>=c for a,c in zip(offsets,offsets[1:])):continue
            pkg=strings[path].decode()
            found[pkg]={'functions':nf,'hash':meta.hex(),'files':[s.decode(errors='replace') for s in strings if s.endswith(b'.go')][:3]}
        except (IndexError,ValueError,struct.error):continue
    return found
def gopcln(b):
    # Go 1.20+ pclntab header from debug/gosym/pclntab.go. External links
    # can drop ELF symbols while retaining the Go function name table.
    for m in re.finditer(rb'\xf1\xff\xff\xff\x00\x00[\x01\x04]\x08',b):
        at=m.start()
        if at+72>len(b):continue
        nfunc,files,text,names,cu,ft,pc,funcs=struct.unpack_from('<8Q',b,at+8)
        if not(1000<nfunc<1000000 and files>100 and 72<=names<cu<=ft<=pc<=funcs<len(b)-at):continue
        nameset=set(b[at+names:at+cu].split(b'\0'))
        if b'runtime.main' in nameset:return nameset
    return set()
def inspect_elf(data,arch,tmpfile):
    assert data[:6]==b'\x7fELF\x02\x01'
    tmpfile.write_bytes(data)
    def run(*args):
        res=subprocess.run(args,capture_output=True,text=True,errors='replace',timeout=65)
        return res.stdout if res.returncode==0 else ''
    ph=run('readelf','-lW',str(tmpfile));dy=run('readelf','-dW',str(tmpfile));cm=run('readelf','-p','.comment',str(tmpfile));note=run('go','version','-m',str(tmpfile))
    mach=struct.unpack_from('<H',data,18)[0]
    assert ph and dy
    names=gopcln(data);g=covmeta(data);pre='gvisor.dev/gvisor/pkg/'
    return dict(machine=mach,correct_machine=mach=={'amd64':62,'arm64':183}[arch],static='INTERP' not in ph and '(NEEDED)' not in dy,
        external_linker=[s.strip()[-160:] for s in cm.splitlines() if 'Linker: LLD' in s],
        pclntab_size=len(names),pclntab_raceinit=b'runtime.raceinit' in names,tsan=b'__tsan_init' in data,race_buildinfo=[s.strip() for s in note.splitlines() if '-race=true' in s],
        coverage_packages=len(g),tcpip=g.get(pre+'tcpip'),sync=g.get(pre+'sync'),sync_linked=sorted(s.decode(errors='replace') for s in names if s.startswith((pre+'sync.').encode()))[:2],coverage_neighbors=[k for k in sorted(g) if k.startswith(pre+'tcpip')][:4])
def run():
    save()
    t=tarfile.open(tmp/'original'/'native-packages.tgz')
    orig=json.load(t.extractfile('summary.json'));assert orig['source']==product and orig['status']=='PASS'
    for arch in ('amd64','arm64'):
        n=arch+'-runsc';source=orig['builds'][n];inv=source['invocation']
        assert source['expected']==source['succeeded']==389
        row=results['architectures'][arch]={'invocation':inv,'binaries':{},'diagnostics':[]}
        events=[json.loads(x) for x in t.extractfile(n+'/events')]
        named={str(e['id']['namedSet']['id']):e['namedSetOfFiles'] for e in events if 'namedSetOfFiles' in e}
        try:
            data=json.loads(read('https://app.buildbuddy.io/rpc/BuildBuddyService/GetExecution',json.dumps({'executionLookup':{'invocationId':inv}}).encode()))
            links=[e for e in data.get('execution',[]) if e.get('actionMnemonic')=='GoLink' and not e.get('status',{}).get('code') and not e.get('exitCode')]
            row['original_remote_links']=len(links)
        except Exception as e:links=[];row['diagnostics'].append(safe(e))
        for e in events:
            target=e['id'].get('targetCompleted',{}).get('label','')
            if target not in ('//runsc:runsc_race_coverage','//runsc/cmd/sentry:gvisor_sentry_race_coverage'):continue
            config=e['id']['targetCompleted']['configuration']['id'];group,=e['completed']['outputGroup'];assert e['completed']['success'] and group['name']=='default'
            fs,=group['fileSets'];f,=named[str(fs['id'])]['files'];name=f['name'].split('/')[-1]
            r=row['binaries'][target]={'name':name,'sha256':f['digest'],'bytes':int(f['length']),'config':config}
            try:
                payload=cas(inv,dict(hash=f['digest'],sizeBytes=f['length']))
                r['elf']=inspect_elf(payload,arch,tmp/name)
                (tmp/name).unlink()
                for l in links:
                    if not l.get('primaryOutputPath','').endswith('/'+name):continue
                    try:r['link']=read_link(inv,l);break
                    except Exception as ex:row['diagnostics'].append(safe(ex))
            except Exception as ex:r['error']=safe(ex)
            save()
        es=[b.get('elf',{}) for b in row['binaries'].values()]
        row['checks']={'both_executables':len(es)==2,
          'machine_and_static':len(es)==2 and all(v.get('correct_machine') and v.get('static') for v in es),
          'external_link':len(es)==2 and all(b.get('link',{}).get('external') or b.get('elf',{}).get('external_linker') for b in row['binaries'].values()),
          'race':len(es)==2 and all(b.get('link',{}).get('race') or b.get('elf',{}).get('pclntab_raceinit') or b.get('elf',{}).get('race_buildinfo') for b in row['binaries'].values()),
          'tcpip_covered_sync_excluded':len(es)==2 and all(v.get('coverage_packages',0)>0 and not v.get('sync') and v.get('sync_linked') for v in es) and any(v.get('tcpip') for v in es)}
        row['status']='PASS' if all(row['checks'].values()) else 'INCOMPLETE'
    results['status']='PASS' if all(a['status']=='PASS' for a in results['architectures'].values()) else 'INCOMPLETE'
try:run()
finally:save()
print(json.dumps(results,indent=2));raise SystemExit(results['status']!='PASS')
