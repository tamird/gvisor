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
 assert len(h)==64 and 0<=n<=maxsize and len(out['lookups'])<95
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
    assert cnt<=n and total<=90*1024*1024
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

writers = [
    ("make_deb", "a68ef9d101d5db1cb71129231b213431a9fd1d9c63b07004012753f12ab549b2", "2e7658975637784fe1895597501899c9336516167c308e60b1c5dd9759252273", 12105),
    ("build_tar", "ea84e1d2adda8d3e6e3cfd8f404dfb64234575eb132e9315ab5cad0442bf8cd9", "920f5d861eb69ba946faaac7b9c9942c71ca061034aeafef9a9dac62b60560d1", 12107),
]
failures = []
for name, action_sha, wrapper_sha, size in writers:
    rec = out["writers"][name] = {}
    try:
        script = get([wrapper_sha, size])
        rec["shebang"] = script.splitlines()[0].decode()
        assert rec["shebang"] == "#!/usr/bin/env bash"
        action = get([action_sha, 351]); root = ident(one(action, 2))
        command = get(ident(one(action, 1)))
        path = next(v.decode() for key, v in fields(command) if key == 1)
        assert path.endswith("/" + name)
        entry = node(root, path)
        assert entry == {"kind": "file", "digest": [wrapper_sha, size]}, entry
        rec.update(root=root, path=path, launcher=entry)
        for suffix in (".runfiles", ".runfiles_manifest"):
            rec[suffix] = node(root, path + suffix)
        runfiles = rec[".runfiles"]
        assert runfiles["kind"] == "dir"
        rec["runfile_roots"] = {
            one(v, 1).decode(): ("file", "dir", "symlink")[kind - 1]
            for kind, v in fields(get(runfiles["digest"])) if kind in (1, 2, 3)
        }
        for candidate in ("external/+_repo_rules+protoc_check_static_python/bin/python3.11",
                          path + ".runfiles/+_repo_rules+protoc_check_static_python/bin/python3.11",
                          path + ".runfiles/_main/external/+_repo_rules+protoc_check_static_python/bin/python3.11"):
            rec.setdefault("static_path_checks", {})[candidate] = node(root, candidate)
    except Exception as exc:
        failures.append(type(exc).__name__ + ": " + str(exc)[:140])
out["failed"] = failures
p = Path(os.environ["RUNNER_TEMP"]) / "original-launchers.json"
p.write_text(json.dumps(out, indent=2) + "\n")
print("original writer records",len(writers),"errors",len(failures))
raise SystemExit(int(bool(failures)))
