"""Read one completed, content-addressed shipping target from its existing invocation."""
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import urlsplit
from urllib.error import HTTPError
import hashlib,io,json,os,re,sys,tarfile
r=Path(sys.argv[1]);r.mkdir(parents=True,exist_ok=True)
inv=sys.argv[2]
def rpc(name,obj):
    req=Request('https://app.buildbuddy.io/api/v1/'+name,data=json.dumps(obj).encode(),headers={'Content-Type':'application/json','x-buildbuddy-api-key':os.environ['BUILDBUDDY_API_KEY']})
    return urlopen(req,timeout=45)
with rpc('GetInvocation',{'selector':{'invocation_id':inv},'include_artifacts':True}) as res:
    v,=json.load(res)['invocation']
assert v['success'] and v['pattern']=='//images/benchmarks/iperf:iperf_context'
outputs={f['uri'] for f in v.get('artifacts',[]) if Path(f['name']).name=='iperf_context.tar'}
uri,=outputs
parts=urlsplit(uri);assert parts.scheme=='bytestream' and parts.hostname=='remote.buildbuddy.io',parts.scheme
m=re.search(r'/blobs/([0-9a-f]{64})/([0-9]+)$',parts.path);assert m
digest,n=m.groups();n=int(n);assert 0<n<48*1024*1024,n
with rpc('GetFile',{'uri':uri}) as res: archive=res.read(n+1)
assert len(archive)==n and hashlib.sha256(archive).hexdigest()==digest
with tarfile.open(fileobj=io.BytesIO(archive),mode='r:*') as ctx:
    member,=[f for f in ctx if f.name.removeprefix('./')=='images/benchmarks/iperf/image.tar']
    image=ctx.extractfile(member).read()
out=Path(os.environ['RUNNER_TEMP'])/'iperf-original-image.tar'
out.write_bytes(image)
expected={'usr/share/doc/iperf3/LICENSE':'3dc3e2076dd9cdea2b66d8fe213997ff25fb6171594f055b59fe25321b438f6f','usr/share/doc/openssl/LICENSE.txt':'7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a'}
files={};dirs={}
with tarfile.open(fileobj=io.BytesIO(image),mode='r:*') as img:
    manifest,=json.load(img.extractfile('manifest.json'))
    assert len(manifest['Layers'])==2
    for layer in manifest['Layers']:
        with tarfile.open(fileobj=io.BytesIO(img.extractfile(layer).read()),mode='r:*') as tar:
            for f in tar:
                name=f.name.removeprefix('./').rstrip('/')
                if f.isdir(): dirs[name]=f.mode;continue
                assert f.isfile(),f.name
                files[name]=tar.extractfile(f).read()
assert set(files)=={'usr/bin/iperf3',*expected} and dirs['tmp']==0o1777
for path,d in expected.items(): assert hashlib.sha256(files[path]).hexdigest()==d
exe=files['usr/bin/iperf3'];assert exe.startswith(b'\x7fELF') and int.from_bytes(exe[18:20],'little')==int(sys.argv[3])
(r/'original-archive.json').write_text(json.dumps({'invocation':inv,'contextSha256':digest,'contextBytes':n,'imageSha256':hashlib.sha256(image).hexdigest(),'binarySha256':hashlib.sha256(exe).hexdigest(),'binaryBytes':len(exe),'notices':expected,'tmpMode':oct(dirs['tmp'])},indent=2)+'\n')
print('Recovered',n,'context bytes and',len(exe),'executable bytes')
