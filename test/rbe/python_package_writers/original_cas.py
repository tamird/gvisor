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


