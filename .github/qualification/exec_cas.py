"""Read the six original TestRunner execution records and verify their action digests."""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


INV = None  # The caller binds this to the sole new source-verified BEP.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(NoRedirect)
deadline = time.monotonic()+120
remaining_bytes = 80*1024*1024
calls = 0


def fetch(path, *, request=None, limit):
    global calls, remaining_bytes
    calls+=1
    assert calls<=90 and time.monotonic()<deadline and remaining_bytes>0
    assert path == "rpc/BuildBuddyService/GetExecution" or path.startswith("file/download?")
    req = urllib.request.Request("https://app.buildbuddy.io/"+path,
        data=None if request is None else json.dumps(request).encode(),
        headers={"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["ACCESS"]})
    cap=min(remaining_bytes,limit)
    with opener.open(req,timeout=min(20,max(1,deadline-time.monotonic()))) as response:
        data=response.read(cap+1)
    remaining_bytes-=len(data)
    assert len(data)<=cap
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


def executions(owner, mnemonic):
    rows=[]
    for label in ((owner,"@@"+owner) if owner.startswith("//") else (owner,)):
        r=json.loads(fetch("rpc/BuildBuddyService/GetExecution",request={
            "executionLookup":{"invocationId":INV,"targetLabel":label},"inlineExecuteResponse":True},limit=4*1024*1024))
        rows=[e for e in r.get("execution",[]) if e.get("targetLabel") in (owner,"@@"+owner)
              and e.get("actionMnemonic")==mnemonic]
        if rows:
            break
    return rows
