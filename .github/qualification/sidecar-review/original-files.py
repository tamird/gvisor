"""Inspect only the original six TestRunner output records; no Run endpoint."""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import tarfile
import traceback
from xml.etree import ElementTree as ET

ARCHIVE=("ce8a162fd07a1f04adf5151930deb3dda68fb9f096d2e727f95748573c9846dc",1151137)
ARMS={"gnu-tests":"02ffd8f9-f490-486e-9033-efdd1ecd1914","clang-tests":"537e792f-01e1-422b-9e48-fddfea868195"}
LABELS=("//runsc/fdparking:fdparking_test","//runsc/prewarmer:prewarmer_single_page_test","//runsc/procumounter:procumounter_test")
sha=lambda b:hashlib.sha256(b).hexdigest()
def request(key,method,q,cap):
    assert method in ("/api/v1/GetFile","/rpc/BuildBuddyService/GetExecution")
    c=http.client.HTTPSConnection("app.buildbuddy.io",timeout=50)
    try:
        c.request("POST",method,json.dumps(q),{"content-type":"application/json","x-buildbuddy-api-key":key})
        with c.getresponse() as res:
            assert res.status==200,res.status
            data=res.read(cap+1);assert len(data)<=cap;return data
    finally:c.close()
def digest(d):
    h=d.get("hash","");n=int(d.get("sizeBytes",0));assert re.fullmatch(r"[0-9a-f]{64}",h) and 0<=n<=4<<20
    return h,n
def get(key,ident):
    h,n=ident;assert re.fullmatch(r"[0-9a-f]{64}",h) and 0<=n<=4<<20
    b=request(key,"/api/v1/GetFile",{"uri":"bytestream://remote.buildbuddy.io/blobs/%s/%d"%(h,n)},n)
    assert (sha(b),len(b))==(h,n);return b
def pfields(b):
    i=0
    def var():
        nonlocal i
        v=0
        for j in range(10):
            u=b[i];i+=1;v|=(u&127)<<(7*j)
            if u<128:return v
        raise ValueError("var")
    while i<len(b):
        k=var();w=k&7
        if w==0:v=var()
        elif w==2:
            n=var();v=b[i:i+n];i+=n;assert len(v)==n
        elif w in (1,5):n=8 if w==1 else 4;v=b[i:i+n];i+=n
        else:raise ValueError("wire")
        yield k>>3,v
def treefiles(tree):
    for field,entry in pfields(tree):
        if field not in (1,2):continue
        for k,node in pfields(entry):
            if k!=1:continue
            n=dict(pfields(node));name=n[1].decode()
            if name not in ("test.xml","test.log"):continue
            d=dict(pfields(n[2]));yield name,(d[1].decode(),int(d.get(2,0)))
def compare(key,name,ident,original):
    rec={"hash":ident[0],"bytes":ident[1],"exact":ident==(sha(original),len(original))}
    if rec["exact"]:return rec
    b=get(key,ident)
    if name=="test.xml":
        try:
            def shape(s):
                return [(n.get("name"),tuple(x.tag for x in n)) for n in ET.fromstring(s).findall(".//testcase")]
            rec["same_case_outcomes"]=shape(b)==shape(original);rec["cases"]=len(shape(b))
        except ET.ParseError:rec["xml"]=False
    else:
        rec["worker_suffix_of_bep"]=original.endswith(b)
        rec["bep_suffix_of_worker"]=b.endswith(original)
    return rec
def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY");out={"archive":ARCHIVE,"arms":{}}
    try:
        raw=get(key,ARCHIVE)
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:
            files={m.name.removeprefix("./"):m for m in t if m.isfile()}
            def read(suffix):
                [m]=[v for k,v in files.items() if k==suffix or k.endswith("/"+suffix)]
                assert m.size<12<<20;return t.extractfile(m).read()
            for phase,child in ARMS.items():
                arm=out["arms"][phase]={};prefix="invocation-"+phase+"/"
                rows=json.loads(read(prefix+"events.jsonl.manifest.json"))["reports"]
                for label in LABELS:
                    rec=arm[label]={"physical_files":[]};inputs={}
                    try:
                        for name in ("test.xml","test.log"):
                            [r]=[i for i in rows if i["name"]==name and i["identity"]["label"]==label]
                            b=read(prefix+r["path"]);assert (sha(b),len(b))==(r["sha256"],r["bytes"]);inputs[name]=b
                        body=request(key,"/rpc/BuildBuddyService/GetExecution",{"executionLookup":{"invocationId":child,"targetLabel":label},"inlineExecuteResponse":True},6<<20)
                        [e]=[e for e in json.loads(body)["execution"] if "test-setup.sh" in e.get("commandSnippet","")]
                        result=e["executeResponse"]["result"]
                        aid=e["actionDigest"]["hash"];assert re.fullmatch("[0-9a-f]{64}",aid)
                        rec["completed"]=e.get("stage") in ("COMPLETED",4)
                        rec["exec_exit"]=int(e.get("exitCode",0))
                        rec["status_codes"]=[int(o.get("status",{}).get("code",0)) for o in (e,e["executeResponse"])]
                        rec["cache_hit"]=bool(e["executeResponse"].get("cachedResult",False))
                        rec.update(action=aid,exit=int(result.get("exitCode",0)),worker=bool(result.get("executionMetadata",{}).get("worker") or e.get("executedActionMetadata",{}).get("worker") or e.get("executorHostname")))
                        rec["result_fields"]=sorted(k for k in result if re.fullmatch(r"[a-zA-Z]{1,65}",k))
                        rec["executed_response_fields"]=sorted(k for k in e["executeResponse"] if re.fullmatch(r"[a-zA-Z]{1,65}",k))
                        for f in result.get("outputFiles",[]):
                            name=f["path"].rsplit("/",1)[-1];rec["physical_files"].append(name if name in inputs else "other")
                            if name in inputs:rec[name]=compare(key,name,digest(f["digest"]),inputs[name])
                        rec["directories"]=len(result.get("outputDirectories",[]))
                        for d in result.get("outputDirectories",[])[:4]:
                            if d.get("treeDigest"):
                                for name,ident in treefiles(get(key,digest(d["treeDigest"]))):
                                    rec.setdefault("tree_files",{})[name]=compare(key,name,ident,inputs[name])
                        for name in ("stdoutDigest","stderrDigest"):
                            if result.get(name):
                                ident=digest(result[name]);rec[name]={"hash":ident[0],"bytes":ident[1],"matches_log":ident==(sha(inputs["test.log"]),len(inputs["test.log"]))}
                        rec["bep"]={n:[sha(b),len(b)] for n,b in inputs.items()}
                    except Exception as e:
                        rec["error"]=type(e).__name__;rec["lines"]=[x.lineno for x in traceback.extract_tb(e.__traceback__) if x.filename.endswith("original-files.py")][-4:]
        assert all(not r.get("error") and r.get("worker") and r.get("completed") and not r.get("cache_hit") and r["exec_exit"]==r["exit"]==0 and r["status_codes"]==[0,0] for a in out["arms"].values() for r in a.values())
        out["status"]="ORIGINAL_ACTION_INVENTORY"
    except Exception as e:
        out["status"]="INCOMPLETE";out["error"]=type(e).__name__
    Path("verdict.json").write_text(json.dumps(out,indent=2)+"\n")
    return int(out["status"]!="ORIGINAL_ACTION_INVENTORY")
if __name__=="__main__":raise SystemExit(main())
