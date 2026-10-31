"""Read the existing application blobs, verify OCI digests and report their exact members."""
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import hashlib,io,json,os,tarfile,sys,subprocess
out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
receipts=json.loads(Path(sys.argv[1]).read_text());summary={}
labels={'profile':'//images/benchmarks/profile-helper:profile_helper_layer','ollama':'//images/gpu/ollama/client:client_layer','hackbench':'//images/benchmarks/hackbench:hackbench_layer','hey':'//images/benchmarks/hey:hey_layer'}
env={k:v for k,v in os.environ.items() if k!='BUILDBUDDY_API_KEY'}
def cached_layer(name,record):
    product=record['source']
    assert len(product)==40 and int(product,16)
    def run(args): return subprocess.check_output(args,env=env,text=True,timeout=270)
    if subprocess.run(['git','cat-file','-e',product+'^{commit}'],env=env).returncode:
        run(['git','fetch','--depth=1','origin',product])
    run(['git','checkout','--quiet','--detach',product])
    kind,arm=name.split('-'); label=labels[kind]
    bz=['bazel','--bazelrc='+os.path.join(os.environ['RUNNER_TEMP'],'images.bazelrc')]
    run([*bz,'build','--experimental_remote_require_cached','--config='+arm,label])
    paths=run([*bz,'cquery','--output=files','--config='+arm,label]).splitlines()
    for path in map(Path,paths):
        if path.is_file() and path.stat().st_size==record['size']:
            data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()==record['digest']:return data
    raise ValueError('cached layer target did not contain the expected OCI digest')
for name,record in receipts.items():
    digest,size=record['digest'],record['size']
    assert 0<size<=8<<20 and len(digest)==64 and int(digest,16)
    uri=f'bytestream://remote.buildbuddy.io/blobs/{digest}/{size}'
    req=Request('https://app.buildbuddy.io/api/v1/GetFile',json.dumps({'uri':uri}).encode(),{'Content-Type':'application/json','x-buildbuddy-api-key':os.environ['BUILDBUDDY_API_KEY']})
    try:
        method='verified OCI digest in BuildBuddy CAS'
        try:
            with urlopen(req,timeout=40) as response:data=response.read(size+1)
        except HTTPError as e:
            if e.code not in (404,410):raise
            method='exact-source Bazel cache hit'
            data=cached_layer(name,record)
        if len(data)!=size or hashlib.sha256(data).hexdigest()!=digest:raise ValueError('returned blob does not match recorded OCI digest')
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as layer:
            entries=[m for m in layer.getmembers() if not m.isdir()]
            assert sum(m.size for m in entries) < 128<<20
            members=[]
            for m in entries:
                row={'path':m.name,'type':m.type.decode(),'bytes':m.size,'link':m.linkname}
                if m.isfile(): row['sha256']=hashlib.sha256(layer.extractfile(m).read()).hexdigest()
                members.append(row)
        extra=[m for m in members if '.runfiles' in m['path'] or '.repo_mapping' in m['path']]
        summary[name]={**record,'retrieval':method,'verified':True,'members':members,'runfiles_count':len(extra),'runfiles_regular_bytes':sum(m['bytes'] for m in extra)}
        print(name,len(members),'members;',len(extra),'generated runfiles;',summary[name]['runfiles_regular_bytes'],'bytes',flush=True)
    except (HTTPError,OSError,ValueError,subprocess.SubprocessError) as e:
        print(name,type(e).__name__,str(e)[:140],flush=True);summary[name]={**record,'verified':False,'error':str(e)[:140]}
    (out/'layer-members.json').write_text(json.dumps(summary,indent=2)+'\n')
if not all(r['verified'] for r in summary.values()):raise SystemExit('At least one original layer was unavailable; no inference from its source.')
