"""Report the exact original action and its resource numbers; omit raw logs."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

from original_worker import first, rpc, safe

inv = "f68018e9-aaa6-4258-a9a0-48b990d14f23"
target = "@@protobuf+//src/google/protobuf:protobuf"
class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None

def blob(digest):
    h=digest["hash"];n=int(first(digest,"sizeBytes","size_bytes"))
    assert re.fullmatch("[0-9a-f]{64}",h) and 0 <= n <= 5_000_000
    q=urllib.parse.urlencode({"invocation_id":inv,"bytestream_url":f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"})
    req=urllib.request.Request("https://app.buildbuddy.io/file/download?"+q,headers={"x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    with urllib.request.build_opener(RefuseRedirect()).open(req,timeout=20) as f:
        data=f.read(n+1)
    assert len(data)==n and hashlib.sha256(data).hexdigest()==h
    return data

def fields(buf):
    vals=[]; i=0
    def number():
        nonlocal i
        val=shift=0
        while i<len(buf):
            b=buf[i];i+=1;val|=(b&127)<<shift
            if b<128:return val
            shift+=7
        raise ValueError("truncated field")
    while i<len(buf):
        tag=number()
        if tag&7==0:v=number()
        elif tag&7==2:
            size=number();v=buf[i:i+size];i+=size
        elif tag&7 in (1,5):
            n=8 if tag&7==1 else 4;v=buf[i:i+n];i+=n
        else:raise ValueError("unexpected wire type")
        vals.append((tag>>3,v))
    return vals

def digest_from(buf):
    d=dict(fields(buf));return {"hash":d[1].decode(),"sizeBytes":d[2]}

def scan_numbers(value):
    numbers={}
    def walk(v,prefix):
        if isinstance(v,dict):
            for key,field in v.items():
                new=prefix+[key]
                if isinstance(field,(dict,list)):walk(field,new)
                elif re.search(r"^(peak.memory.bytes|estimated.memory.bytes|estimated.milli.cpu|memory.max|memory.high|oom.kills?|oom.kill.count|oom.count|memory.limit.bytes|memory.limit.events|cpu.nanos)$",key.replace("_","."),re.I) or key in ("peakMemoryBytes","estimatedMemoryBytes","estimatedMilliCpu","oomKillCount","oomCount","memoryMax","memoryHigh","cpuNanos"):
                    if isinstance(field,int) or re.fullmatch("-?[0-9]+",str(field)):
                        numbers[".".join(new)]=int(field)
        elif isinstance(v,list):
            for item in v:walk(item,prefix)
    walk(value,[]);return numbers

def selected_platform(buf):
    out={}
    for prop,raw in fields(buf):
        if prop!=1:continue
        d=dict(fields(raw));k=d[1].decode()
        if k in ("Arch","OSFamily","container-image","workload-isolation-type","EstimatedMemory","EstimatedMemoryBytes","EstimatedCPU","EstimatedComputeUnits"):
            out[k]=safe(d.get(2,b"").decode())
    return out

out=[]
r=rpc("GetExecution",{"executionLookup":{"invocationId":inv,"targetLabel":target}})
for ex in r.get("execution",[]):
    if ex.get("actionMnemonic")!="CppCompile" or not ex.get("exitCode"):continue
    dig=ex["actionDigest"]
    row={"action":dig,"exit":int(ex["exitCode"]),"rpc_status":int(ex.get("status",{}).get("code",0)),"metrics":scan_numbers(ex)}
    try:
        act=dict(fields(blob(dig)));command=fields(blob(digest_from(act[1])))
        row["cc_inputs"]=[value.decode().split("/")[-1] for key,value in command if key==1 and isinstance(value,bytes) and value.endswith(b".cc")]
        row["platform"]=selected_platform(act.get(10,dict(command).get(5,b"")))
    except Exception as e:
        row["action_fetch_error"]=safe(str(e))
    rr=rpc("GetExecution",{"executionLookup":{"invocationId":inv,"actionDigestHash":dig["hash"]},"inlineExecuteResponse":True})
    found=next((f for f in rr.get("execution",[]) if f.get("actionDigest",{}).get("hash")==dig["hash"]),{})
    resp=first(found,"executeResponse","execute_response") or {}
    row["metrics"].update(scan_numbers(resp))
    result=resp.get("result",{})
    ds=first(result,"stderrDigest","stderr_digest")
    if ds:
        row["stderr_digest"]=ds
        try:
            row["compiler_stderr"]=[safe(l) for l in blob(ds).decode(errors="replace").splitlines() if "fatal error:" in l or "compilation terminated" in l][:5]
        except Exception as e:
            row["stderr_fetch_error"]=safe(str(e))
    out.append(row)
print(json.dumps(out,indent=2))
Path("original-compiler-diagnostic.json").write_text(json.dumps({"invocation":inv,"failed_actions":out},indent=2)+"\n")
