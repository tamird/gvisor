"""Read the failing remote command and its actual go_merge input from CAS."""
import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request


INV = "d884c539-03fa-4d16-a433-db09c04e3e9d"
OWNER = "//pkg/ring0/pagetables:generic_walker_arm64"
TOOL = "//tools/go_generics/go_merge:go_merge"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(NoRedirect)


def fetch(path, *, request=None, limit):
    assert path in ("rpc/BuildBuddyService/GetExecution","rpc/BuildBuddyService/GetEventLogChunk") or path.startswith("file/download?")
    req = urllib.request.Request("https://app.buildbuddy.io/"+path,
        data=None if request is None else json.dumps(request).encode(),
        headers={"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["ACCESS"]})
    with opener.open(req,timeout=20) as response:
        data=response.read(limit+1)
    assert len(data)<=limit
    return data


def blob(digest, limit):
    h,size=digest["hash"],int(digest["sizeBytes"])
    assert re.fullmatch(r"[0-9a-f]{64}",h) and 0<size<=limit
    uri="bytestream://remote.buildbuddy.io/blobs/"+h+"/"+str(size)
    data=fetch("file/download?"+urllib.parse.urlencode({"invocation_id":INV,"bytestream_url":uri}),limit=size)
    assert len(data)==size and hashlib.sha256(data).hexdigest()==h
    return data


def varint(data, pos):
    value=0
    for shift in range(0,70,7):
        byte=data[pos]
        pos+=1
        value|=(byte&127)<<shift
        if byte<128:
            return value,pos
    raise ValueError("varint")


def fields(data):
    pos=0
    while pos<len(data):
        tag,pos=varint(data,pos)
        wire=tag & 7
        if wire==0:
            value,pos=varint(data,pos)
        else:
            if wire==2:
                size,pos=varint(data,pos)
            else:
                size={1:8,5:4}[wire]
            assert pos+size<=len(data)
            value=data[pos:pos+size]
            pos+=size
        yield tag>>3,value


def digest_msg(data):
    values=dict(fields(data))
    return {"hash":values[1].decode("ascii"),"sizeBytes":values[2]}


def identified_executable(raw):
    argv0=raw.decode("ascii")
    p=PurePosixPath(argv0)
    assert not p.is_absolute() and p.parts[0]=="bazel-out" and ".." not in p.parts
    assert len(argv0)<=370 and re.fullmatch(r"bazel-out/[A-Za-z0-9_+./-]+/bin/tools/go_generics/go_merge/(?:[A-Za-z0-9._+-]+/)*go_merge",argv0)
    return p


def native(raw):
    assert raw[:6]==b"\x7fELF\x02\x01"
    machine,=struct.unpack_from("<H",raw,18)
    start,=struct.unpack_from("<Q",raw,32)
    size,count=struct.unpack_from("<HH",raw,54)
    assert 0<count<=256 and start+count*size<=len(raw)
    interp=None
    for i in range(count):
        kind,_,off,_,_,n,_,_=struct.unpack_from("<II6Q",raw,start+i*size)
        if kind==3:
            assert 1<n<=128 and off+n<=len(raw)
            value=raw[off:off+n].rstrip(b"\0").decode("ascii")
            assert re.fullmatch(r"/(?:[A-Za-z0-9._+-]+/)*ld[A-Za-z0-9._+-]+",value)
            interp=value
    return {"machine":machine,"interpreter":interp,"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)}


def executions(owner, mnemonic):
    rows=[]
    for label in (owner,"@@"+owner):
        r=json.loads(fetch("rpc/BuildBuddyService/GetExecution",request={
            "executionLookup":{"invocationId":INV,"targetLabel":label},"inlineExecuteResponse":True},limit=4*1024*1024))
        rows=[e for e in r.get("execution",[]) if e.get("targetLabel") in (owner,"@@"+owner)
              and e.get("actionMnemonic")==mnemonic]
        if rows:
            break
    return rows


def evidence(data):
    lines=data.splitlines()
    return {
        "enoent":any(b"no such file or directory" in s.lower() for s in lines),
        "start_enoent":any(b"go_merge" in s and b"no such file or directory" in s.lower() and
                           re.search(rb"(exec|start|launch|failed)",s,re.I) for s in lines),
        "wrong_format":any(b"exec format" in s.lower() for s in lines),
        "loader_error":any(b"error while loading shared libraries" in s.lower() for s in lines),
    }


result={"invocation":INV,"owner":OWNER,"producer_source":"83f2d8568e1799eab47b02dc92da2988bdb001d9","status":"INCOMPLETE"}
try:
    result["phase"]="failed-action"
    candidates=executions(OWNER,"GoGenericsTemplate")
    assert len(candidates)==1
    action,=candidates
    meta=action["actionDigest"]
    result["action_digest"]={k:meta[k] for k in ("hash","sizeBytes")}
    reply=action.get("executeResponse",{})
    reply_result=reply.get("result",{})
    result["exit"]=reply_result.get("exitCode")
    result["rpc_status"]=reply.get("status",{}).get("code",0)
    first=action.get("commandSnippet","").partition(" ")[0]
    if first.startswith("bazel-out/") and first.endswith("/go_merge"):
        result["executable"]=str(identified_executable(first.encode()))
    stderr=base64.b64decode(reply_result.get("stderrRaw",""))
    if not stderr and int(reply_result.get("stderrDigest",{}).get("sizeBytes",0))>0:
        stderr=blob(reply_result["stderrDigest"],512*1024)
    result["stderr"]=evidence(stderr+str(reply.get("status",{}).get("message","")).encode())
    result["phase"]="original-command"
    ac=dict(fields(blob(meta,128*1024)))
    command=blob(digest_msg(ac[1]),4*1024*1024)
    executable=identified_executable(next(v for k,v in fields(command) if k==1))
    result["executable"]=str(executable)
    result["phase"]="original-input-tree"
    root=digest_msg(ac[2])
    for i, component in enumerate(executable.parts):
        last=i+1==len(executable.parts)
        directory=blob(root,3*1024*1024)
        node,=[m for k,v in fields(directory) if k==(1 if last else 2)
               and (m:=dict(fields(v))).get(1)==component.encode()]
        root=digest_msg(node[2])
    result["elf"]=native(blob(root,40*1024*1024))
    result["identity"]="command-input"
    result["status"]="PASS"
except (AssertionError,KeyError,IndexError,StopIteration,ValueError,OSError,struct.error) as err:
    result["error"]=type(err).__name__
    if isinstance(err,urllib.error.HTTPError):
        result["http_status"]=err.code
    # If the worker evicted an input directory, try only the corresponding
    # executable output from the same invocation and execution configuration.
    if result.get("executable"):
        try:
            matches=[f for e in executions(TOOL,"GoLink") for f in
                     e.get("executeResponse",{}).get("result",{}).get("outputFiles",[])
                     if f.get("path")==result["executable"]]
            file,=matches
            result["elf"]=native(blob(file["digest"],40*1024*1024))
            result["identity"]="same-invocation-GoLink-exact-path"
            result["status"]="PASS"
        except (AssertionError,KeyError,ValueError,OSError,struct.error) as secondary:
            result["link_recovery_error"]=type(secondary).__name__
if not result.get("stderr",{}).get("start_enoent"):
    try:
        raw=json.loads(fetch("rpc/BuildBuddyService/GetEventLogChunk",request={"invocationId":INV,"minLines":2000},limit=16*1024*1024))
        result["log"]=evidence(base64.b64decode(raw["buffer"]))
    except (KeyError,ValueError,OSError) as exc:
        result["log_recovery_error"]=type(exc).__name__
raw=json.dumps(result,indent=2).encode()
assert len(raw)<4096 and os.environ["ACCESS"].encode() not in raw
with open(sys.argv[1],"wb") as output:
    output.write(raw)
print("Original generator diagnostic",result["status"])
