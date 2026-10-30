"""Resolve just the frontend tree in the original REAPI failure (no builds)."""
from functools import cache
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

ACTION = ("86478f15c73a58260719e218c04559887ba65fe3b038ce1e6459bee0567aa4eb",353)
out = Path(os.environ["RUNNER_TEMP"]) / "publication"
out.mkdir(exist_ok=True)
result = {"action": ACTION}

@cache
def blob(h,n):
    assert 0 <= n <= 4<<20 and blob.cache_info().misses < 96
    uri = f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"
    req = Request("https://app.buildbuddy.io/api/v1/GetFile", json.dumps({"uri":uri}).encode(), {
        "Content-Type":"application/json", "x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    with urlopen(req, timeout=20) as u: b=u.read(n+1)
    assert len(b)==n and hashlib.sha256(b).hexdigest()==h
    return b

def fields(b):
    i=0
    def vint():
        nonlocal i
        v=0
        for s in range(0,64,7):
            c=b[i]; i+=1; v |= (c & 127)<<s
            if c<128: return v
        raise ValueError("invalid varint")
    f={}
    while i<len(b):
        kind=vint()
        if kind&7==0: v=vint()
        elif kind&7==2:
            n=vint(); v=b[i:i+n]; i+=n
            assert len(v)==n
        elif kind&7 in (1,5):
            n=8 if kind&7==1 else 4; v=b[i:i+n]; i+=n
        else: raise ValueError("invalid wire type")
        f.setdefault(kind>>3,[]).append(v)
    return f

def digest(b):
    d=fields(b)
    return d[1][0].decode(), d.get(2,[0])[0]

def contents(d):
    fs=fields(blob(*d))
    return {n[1][0].decode():(k, digest(n[2][0]) if k!=3 else n[2][0].decode()) for k in [1,2,3] for item in fs.get(k,[]) for n in [fields(item)]}

def descend(root,path):
    d=root
    for part in path.split("/"):
        node=contents(d)
        if part not in node:
            return None
        k,d=node[part]
        if k!=2: return (k,d)
    return (2,d)

try:
    a=fields(blob(*ACTION))
    root=digest(a[2][0])
    c=fields(blob(*digest(a[1][0])))
    args=[x.decode() for x in c[1]]
    cmd_args={}
    for key in ["--frontend","--bootstrap"]:
        cmd_args[key]=args[args.index(key)+1]
    result["selected_arguments"]=cmd_args
    env=dict((d[1][0].decode(),d.get(2,[b''])[0].decode()) for row in c.get(2,[]) for d in [fields(row)])
    result["environment"]={k:env[k] for k in ["RUNFILES_DIR","RUNFILES_MANIFEST_FILE","PYTHONHOME","PYTHONPATH","PATH"] if k in env}
    rf=cmd_args["--frontend"].split(".runfiles/")[0]+".runfiles"
    kind,d=descend(root,rf); assert kind==2
    rfiles=contents(d)
    result["runfiles_roots"]=[n for n in sorted(rfiles) if n.startswith("_main") or "python" in n or "build-" in n][:96]
    venv=cmd_args["--frontend"][len(rf)+1:].rsplit("/bin/",1)[0]
    result["venv"]=venv
    path=venv+"/lib/python3.11/site-packages"
    k,site=descend(d,path); assert k==2
    sel=contents(site)
    result["venv_site_packages"]=sorted(sel)[:65]
    result["wheel_main"]=None
    for n in rfiles:
        if "build" in n and "1_6_1" in n:
            for p in [n+"/site-packages/build",n+"/build"]:
                item=descend(d,p)
                if item and item[0]==2:
                    result["wheel_main"]={"path":p,"files":sorted(contents(item[1]))[:30]}
    for fname in ["_bazel_site_init.py","bazel.pth"]:
        if fname in sel and sel[fname][0]==1:
            b=blob(*sel[fname][1]).decode()
            result[fname]=[l[:350] for l in b.splitlines() if l.startswith("_IMPORTS_STR") or l.startswith("_SELF_RUNFILES_RELATIVE_PATH") or fname=="bazel.pth"][:4]
    if "build" in sel:
        k,raw=sel["build"]
        result["build_site_entry"]=[k,sorted(contents(raw))[:30] if k==2 else raw]
    print(json.dumps(result,indent=2))
finally:
    (out/"original-action.json").write_text(json.dumps(result,indent=2)+"\n")
