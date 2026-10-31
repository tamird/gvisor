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

inv = ""
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
