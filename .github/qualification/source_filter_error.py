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
# CppCompileAction.describeKey prints one Command and one Argument per line.
# When they differ, ActionConflictException prints both descriptions. Compare
# the two before exporting only the changed compiler arguments.
from difflib import SequenceMatcher
def clean_argument(arg):
    if any(k in arg.lower() for k in ("token","password","private_key","credential","remote_header","remote_cache_header","authorization","api_key","cookie")):return "[redacted]"
    return safe(arg)
def compare_args(lhs,rhs):
    edits=[]
    for op,a,b,c,d in SequenceMatcher(None,lhs,rhs,autojunk=False).get_opcodes():
        if op=="equal":continue
        edits.append({"a":[clean_argument(x) for x in lhs[a:b][:12]],"b":[clean_argument(x) for x in rhs[c:d][:12]],
                      "counts":[b-a,d-c]})
        if len(edits)>=18:break
    return edits
def comparison(text):
    lines=text.splitlines()
    meta={}
    commands=[]
    fields={"Label","RuleClass","Configuration","Mnemonic","Action key","IsShareable","Progress message","PrimaryInput","PrimaryOutput","Owner information","Aspects"}
    for line in lines[1:]:
        if line.startswith("  Command: "):
            commands.append({"cmd":line.removeprefix("  Command: "),"args":[],"includes":[],"env":[]})
        elif commands and line.startswith("  Argument: "):commands[-1]["args"].append(line.removeprefix("  Argument: "))
        elif commands and line.startswith("  Declared include source: "):commands[-1]["includes"].append(line.removeprefix("  Declared include source: "))
        elif commands and line.lstrip().startswith(("Environment: ","Environment variable: ")):commands[-1]["env"].append(line.strip())
        else:
            name,sep,value=line.partition(": ")
            if sep and name in fields:
                if name in ("Configuration","Action key"):
                    pieces=value.strip().split(", ")
                    meta[name]=[v if re.fullmatch("[0-9a-f]{32,128}",v) else safe(v) for v in pieces]
                elif name=="Owner information":
                    meta[name]=re.findall(r"(?:label|executionPlatformLabel)=[a-zA-Z0-9_@/+:.-]{1,190}|[0-9a-f]{64}",value)[:12]
                else:meta[name]=safe(value)
    if meta.get("Mnemonic")!="CppCompile" or not commands or "PrimaryInput" not in meta:return None
    out={"fields":meta,"descriptions":len(commands),"commands":[clean_argument(c["cmd"]) for c in commands[:2]],
         "argument_counts":[len(c["args"]) for c in commands[:2]],"env_reported":any(c["env"] for c in commands),
         "include_counts":[len(c["includes"]) for c in commands[:2]]}
    if len(commands)==2:
        a,b=commands
        for kind in ["args","includes","env"]:
            out[kind+"_diff"]=compare_args(a[kind],b[kind])
    # A matching describeKey is printed once by addStringDetail.
    return out
try:
    paths=["external/llvm+/runtimes/glibc/_objs/libm/m.pic.o","external/llvm+/runtimes/_objs/empty/empty.pic.o"]
    cursor="";chunks=[];REPORT["console"]={"chunks":[],"collisions":{}}
    for page in range(8):
        args={"invocationId":IID,"minLines":400}
        if cursor:args["chunkId"]=cursor
        log=read("GetEventLogChunk",args,8*1024**2);raw=base64.b64decode(log.get("buffer",""),validate=True)
        REPORT["console"]["chunks"].append([len(raw),hashlib.sha256(raw).hexdigest()])
        chunks.insert(0,raw)
        joined=re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",b"".join(chunks).decode(errors="replace")).replace("\r","\n")
        for item in paths:
            if item in REPORT["console"]["collisions"]:continue
            # Complete comparisons span many repeated runfile and include paths.
            header="ERROR: file '"+item+"' is generated by these conflicting actions:"
            for part in joined.split(header)[1:]:
                part=part.split("\nERROR:",1)[0]
                if got:=comparison(header+"\n"+part):
                    REPORT["console"]["collisions"][item]=got;break
        cursor=log.get("previousChunkId","")
        if len(REPORT["console"]["collisions"])==len(paths) or not cursor:break
        if not re.fullmatch("[a-zA-Z0-9_-]{1,32}",cursor):raise ValueError("invalid cursor")
except (OSError,ValueError,KeyError,AssertionError) as exc:REPORT["errors"].append(["console",getattr(exc,"code",type(exc).__name__)])
out=json.dumps(REPORT,separators=(",",":")).encode()
assert len(out)<75000 and KEY.encode() not in out
Path(os.environ["RUNNER_TEMP"],"original-filter-error.json").write_bytes(out)
