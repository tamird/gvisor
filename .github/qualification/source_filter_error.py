"""Recover the isolated pre-execution failure. Do not export logs or command lines."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

IID="d55f7927-f0e7-46f3-93ea-be5a5c9d24b9"
KEY=os.environ.pop("BUILDBUDDY_API_KEY")
REPORT={"invocation":IID,"errors":[]}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None
opener=urllib.request.build_opener(NoRedirect)
def read(name,args,size):
    assert (name=="GetInvocation" and args=={"lookup":{"invocationId":IID}}) or (
        name=="GetEventLogChunk" and args["invocationId"]==IID and set(args)<={"chunkId","invocationId","minLines"})
    req=urllib.request.Request("https://remote.buildbuddy.io/rpc/BuildBuddyService/"+name,
        data=json.dumps(args).encode(),headers={"Content-Type":"application/json","x-buildbuddy-api-key":KEY})
    with opener.open(req,timeout=45) as response:
        data=response.read(size+1)
    if len(data)>size:raise ValueError("response too large")
    return json.loads(data)
def safe(s):
    if not isinstance(s,str):return ""
    s=re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",s).replace(KEY,"[redacted]")
    s=re.sub(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s\]\[<>()'\"<>]+","[URL]",s)
    s=re.sub(r"(?i)(?:--)?(?:remote_header|remote_cache_header|remote_exec_header|bes_header|authorization|proxy-authorization|x-buildbuddy-api-key|password|credential|secret|access_token|token|cookie|sig|signature)\s*[:=]\s*\S+","[redacted]",s)
    s=re.sub(r"(?i)\b(?:bearer|basic)\s+\S+|github_pat_\w+|gh[pousr]_\w+|AKIA[A-Z0-9]{16}|\beyJ[A-Za-z0-9_.-]{15,}","[redacted]",s)
    s=re.sub(r"\b[A-Za-z0-9_+/=-]{44,}\b","[opaque value]",s)
    s=re.sub(r"/(?:home/[^/ ]+|root)/[^\s:]*?/execroot/[^/ ]+/","",s)
    s=re.sub(r"/(?:home/[^/ ]+|root)/[^\s:]*?/external/","external/",s)
    s=re.sub(r"(?i)\s+\(cd\s+.*|\s+exec env\s+.*|\s+--remote_(?:header|cache|executor).*","",s)
    return "".join(c for c in s if c.isprintable())[:900]
def reason(data):
    if not isinstance(data,dict):return None
    out={}
    if text:=safe(data.get("message","")):out["message"]=text
    for key,v in data.items():
        if key=="message" or not re.fullmatch(r"[a-zA-Z_]{1,40}",key) or not isinstance(v,dict):continue
        code=str(v.get("code",""))
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}|[0-9]{1,6}",code):out[key]=code
    return out or None
try:
    data=read("GetInvocation",{"lookup":{"invocationId":IID}},30*1024**2)
    inv,=data["invocation"]; assert inv["invocationId"]==IID
    report=REPORT["build"]={"events":len(inv.get("event",[])),"aborts":[]}
    for e in inv.get("event",[]):
        be=e.get("buildEvent",{});en=be.get("finished",{})
        if en:
            report["finish_code"]=str(en.get("exitCode",{}).get("code",0))[:5]
            if f:=reason(en.get("failureDetail")):report["failure"]=f
        ab=be.get("aborted",{});text=safe(ab.get("description",""))
        if text and text not in report["aborts"] and len(report["aborts"])<4:report["aborts"].append(text)
except (OSError,ValueError,KeyError,AssertionError) as exc:REPORT["errors"].append(["build",getattr(exc,"code",type(exc).__name__)])
try:
    report=REPORT["console"]={"chunks":[],"first_errors":[]}
    chunks=[];cursor=""
    for _ in range(4):
        args={"invocationId":IID,"minLines":400}
        if cursor:args["chunkId"]=cursor
        log=read("GetEventLogChunk",args,8*1024**2)
        raw=base64.b64decode(log.get("buffer",""),validate=True)
        chunks.append(raw);report["chunks"].append([len(raw),hashlib.sha256(raw).hexdigest()])
        cursor=log.get("previousChunkId","")
        if not cursor:break
        if not re.fullmatch("[a-zA-Z0-9_-]{1,32}",cursor):raise ValueError("invalid cursor")
    report["complete"]=not bool(cursor)
    for raw in reversed(chunks):
        lines=re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",raw.decode(errors="replace")).splitlines()
        for i,line in enumerate(lines):
            if not re.match(r"^\s*(?:ERROR:|FATAL:|Error (?:while|in|loading)|(?:Caused by:))",line):continue
            text=safe(line)
            if text and text not in report["first_errors"] and len(report["first_errors"])<10:
                report["first_errors"].append(text)
                # Include source error details such as a Starlark traceback or
                # multiline repository mapping diagnostic. Never export a command.
                for nxt in lines[i+1:i+4]:
                    if not re.match(r"^\s+(?:at |File |Error |Traceback|Caused by:|no such |expected |missing |Label |Repository |Target |Unable )",nxt):break
                    clean=safe(nxt)
                    if clean and clean not in report["first_errors"] and len(report["first_errors"])<10:
                        report["first_errors"].append(clean)
except (OSError,ValueError,KeyError,AssertionError) as exc:REPORT["errors"].append(["console",getattr(exc,"code",type(exc).__name__)])
out=json.dumps(REPORT,separators=(",",":")).encode()
assert len(out)<20000 and KEY.encode() not in out
Path(os.environ["RUNNER_TEMP"],"original-filter-error.json").write_bytes(out)
