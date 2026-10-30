"""Read the exact two failed writer actions and their first executable bytes."""
import hashlib
import http.client
import json
import os
from pathlib import Path

key = os.environ.pop("BUILDBUDDY_API_KEY")
out = {"invocation": "84c54fbf-af66-46ea-89b6-c040d7479dea", "lookups": [], "writers": {}}
def fields(data):
 pos=0
 def var():
  nonlocal pos
  val=0
  for n in range(10):
   ch=data[pos];pos+=1;val|=(ch&127)<<(7*n)
   if ch<128:return val
  raise ValueError('bad varint')
 while pos<len(data):
  tag=var()
  if tag&7==0:v=var()
  elif tag&7==2:n=var();v=data[pos:pos+n];pos+=n
  else:raise ValueError('unexpected metadata field')
  yield tag>>3,v
def one(data,k,default=None):
 vs=[v for n,v in fields(data) if n==k];assert len(vs)<=1
 return vs[0] if vs else default
def ident(d):return [one(d,1).decode(),one(d,2,0)]
def sha(b):return hashlib.sha256(b).hexdigest()
seen={};total=0
def get(dg,maxsize=4*1024*1024,head=False):
 global total
 h,n=dg
 assert len(h)==64 and 0<=n<=maxsize and len(out['lookups'])<260
 if (h,n) in seen:return seen[h,n]
 for inst in ('','bb-snapshot/'):
  uri='bytestream://remote.buildbuddy.io/'+inst+'blobs/'+h+'/'+str(n)
  con=http.client.HTTPSConnection('app.buildbuddy.io',timeout=45)
  try:
   con.request('POST','/api/v1/GetFile',json.dumps({'uri':uri}),{'Content-Type':'application/json','x-buildbuddy-api-key':key})
   resp=con.getresponse();l={'digest':dg,'instance':inst,'http':resp.status};out['lookups'].append(l)
   if resp.status!=200:continue
   g=hashlib.sha256();data=bytearray();cnt=0
   while chunk:=resp.read(256*1024):
    cnt+=len(chunk);total+=len(chunk)
    assert cnt<=n and total<=250*1024*1024
    g.update(chunk)
    if not head:data.extend(chunk)
    elif len(data)<64:data.extend(chunk[:64-len(data)])
   assert cnt==n and g.hexdigest()==h
   s=bytes(data);seen[h,n]=s;return s
  finally:con.close()
 raise ValueError('original CAS data unavailable: '+repr(dg))
def node(root, path):
    parts = path.split("/")
    assert parts and all(p not in ("", ".", "..") for p in parts)
    for index, part in enumerate(parts):
        matches = [(kind, value) for kind, value in fields(get(root))
                   if kind in (1, 2, 3) and one(value, 1).decode() == part]
        if not matches:
            return None
        [(kind, value)] = matches
        if kind == 3:
            return {"kind": "symlink", "target": one(value, 2).decode(), "remaining": parts[index + 1:]}
        root = ident(one(value, 2))
        if index == len(parts) - 1:
            return {"kind": "file" if kind == 1 else "dir", "digest": root}
        assert kind == 2


# Read exact CAS input roots rather than relying on the flat execution-log
# renderer, which intentionally does not expand a tool's runfiles tree.
import io
import struct
import subprocess
import zipfile

ARCH=os.environ["QUALIFICATION_ARCH"]
p=Path(os.environ["RUNNER_TEMP"])
stock=json.loads(Path("test/rbe/python_package_writers/ordinary.json").read_text())[ARCH]
sources=json.loads(Path("test/rbe/python_package_writers/writer-digests.json").read_text())
out.update(arch=ARCH, source=os.environ["CHECK_HEAD"], invocation=os.environ["CHECK_INVOCATION"], status="INCOMPLETE")
physical=subprocess.check_output([os.environ["BB"],"print","--compact_execution_log="+str(p/"physical.binpb")],text=True)
rows={}
while physical.strip():
    physical=physical.lstrip();r,n=json.JSONDecoder().raw_decode(physical);physical=physical[n:]
    if (kind:=r.get("mnemonic")) in stock:
        assert kind not in rows
        rows[kind]=r
def verify(kind,row):
    rec=out["writers"][kind]={}
    props={p["name"]:p["value"] for p in row["platform"]["properties"]}
    rec.update(target=row["targetLabel"],platform=props,runner=row["runner"],cache_hit=row.get("cacheHit",False),exit=row.get("exitCode",0),metrics=row.get("metrics"))
    assert props["Arch"]==ARCH and props["container-image"]=="docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
    assert rec["runner"]=="remote" and not rec["cache_hit"] and rec["exit"]==0
    args,input_files,products=stock[kind]
    assert row["commandArgs"][1:]==args
    dg=row["digest"]
    rec["action"]=[dg["hash"],int(dg["sizeBytes"])]
    ac=get(rec["action"]);root=rec["input_root"]=ident(one(ac,2))
    cmd=get(ident(one(ac,1)))
    command=[v.decode() for k,v in fields(cmd) if k==1]
    assert command==row["commandArgs"]
    rec["argv"]=command
    rec["data_inputs"]={}
    for path,d in input_files.items():
        x=node(root,path)
        assert x=={"kind":"file","digest":[d["hash"],int(d["sizeBytes"])]},(path,x,d)
        rec["data_inputs"][path]=x["digest"]
    exe=command[0];xn=node(root,exe);assert xn["kind"]=="file",xn
    exe_bytes=get(xn["digest"]);assert exe_bytes[:5]==b"\x7fELF\x02"
    mach=int.from_bytes(exe_bytes[18:20],"little")
    assert mach=={"amd64":62,"arm64":183}[ARCH],(ARCH,mach)
    off=struct.unpack_from("<Q",exe_bytes,32)[0];sz,count=struct.unpack_from("<HH",exe_bytes,54)
    assert all(struct.unpack_from("<I",exe_bytes,off+sz*i)[0]!=3 for i in range(count))
    rec["launcher"]={"digest":xn["digest"],"elf_machine":mach,"has_pt_interp":False}
    sub="tar" if kind=="PackageTar" else "deb"
    pp=exe+".runfiles/"
    runtimerepo="+_repo_rules+protoc_check_static_python"+("_arm64" if ARCH=="arm64" else "")
    rec["runtime"]=node(root,pp+runtimerepo+"/bin/python3.11")
    z=node(root,pp+"_main/test/rbe/python_package_writers/"+sub+"_archive.zip")
    assert rec["runtime"] and rec["runtime"]["kind"]=="file" and z["kind"]=="file",(rec["runtime"],z)
    rec["archive"]=z["digest"]
    with zipfile.ZipFile(io.BytesIO(get(z["digest"],96*1024*1024))) as za:
        member,=[s for s in za.namelist() if s.endswith("/"+sources[kind]["path"])]
        actual=sha(za.read(member))
        assert actual==sources[kind]["sha256"]
        rec["writer_source"]={"path":member,"sha256":actual}
        rec["zip_member_count"]=len(za.namelist())
        rec["zip_uncompressed_bytes"]=sum(m.file_size for m in za.infolist())
    rec["outputs"]={}
    for f in row["actualOutputs"]:
        n=Path(f["path"]).name;d=f["digest"]
        assert d==products[n],(n,d,products[n])
        product=get([d["hash"],int(d["sizeBytes"])])
        assert sha(product)==d["hash"]
        rec["outputs"][n]=d
    assert rec["outputs"]==products
failed=[]
assert set(rows)==set(stock),rows.keys()
for kind,row in rows.items():
    try:verify(kind,row)
    except Exception as e:failed.append(kind+": "+repr(e))
out.update(status="FAIL" if failed else "PASS",failures=failed)
(p/"closure.json").write_text(json.dumps(out,indent=2)+"\n")
if failed:raise RuntimeError(failed)
