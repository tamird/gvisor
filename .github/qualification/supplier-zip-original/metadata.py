"""Original fixed-origin bounded RE input metadata reader."""
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

def get(dg,maxsize=4*1024*1024,head=False):
 global total
 h,n=dg
 assert len(h)==64 and 0<=n<=maxsize and len(out['lookups'])<112
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
    assert cnt<=n and total<=480*1024*1024
    g.update(chunk)
    if not head:data.extend(chunk)
    elif len(data)<64:data.extend(chunk[:64-len(data)])
   assert cnt==n and g.hexdigest()==h
   s=bytes(data);seen[h,n]=s;return s
  finally:con.close()
 raise ValueError('original CAS data unavailable: '+repr(dg))

def file_at(root,path):
 trail=[];path=posixpath.normpath(path)
 while True:
  assert not path.startswith('/') and '..' not in path.split('/') and len(trail)<15 and path not in trail
  trail.append(path);parts=path.split('/');current=root
  for i,p in enumerate(parts):
   ds=get(current);hits=[(n,v) for n,v in fields(ds) if n in (1,2,3) and one(v,1).decode()==p]
   assert len(hits)==1,(p,trail);n,v=hits[0]
   if n==2:current=ident(one(v,2));continue
   if n==3:
    target=one(v,2).decode();assert not target.startswith('/')
    path=posixpath.normpath(posixpath.join(*([*parts[:i],target,*parts[i+1:]])));break
   assert i==len(parts)-1
   props=one(v,6,b'');mode=one(props,3)
   return {'paths':trail,'digest':ident(one(v,2)),'is_executable':bool(one(v,4,0)),'unix_mode':one(mode,1,0) if mode is not None else None}
  else:raise ValueError('expected a regular file')
