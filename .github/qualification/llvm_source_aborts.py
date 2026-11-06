"""Read only the prior source invocation's console and five target records."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

IID="bc29a60b-916d-49ee-b194-acb6c21cf98f"
TARGETS=["//runsc:version_test","//runsc/gofer/net:netgofer_test","//runsc/boot/filter:filter_precompiled",
         "//runsc/gofer/net/filter:filter_precompiled","//test/syscalls:uname_test_native"]
WORDS=["analysis of target","failed","error:","unexpected","was referenced","not compatible","incompatible","no matching","no such",
       "missing","not found","cannot","unavailable","not available","not visible","not permitted","was not built","does not provide",
       "prerequisite","looked up","target platform","execution platform","toolchain","cycle","select()","visibility",
       "configuration","configured target","environment","aspect","starlark","aborted","cancelled","canceled","skipped","not run",
       "transitive","errors encountered","does not exist","omitted","not supported","input file","unsuccessful","does not match",
       "incomplete","unavailable","local fallback","failed to build","request was rejected","content digest","invalid",
       "QEMU","qemu-aarch64","qemu-x86_64","filter_precompiled","_run_generator","exec format","CppCompile",
       "RegisterRef","std::less","make_transparent.h","readelf","TestCollectLinksAndRoutes"]
ENUM=re.compile(r"[A-Z][A-Z_]{0,55}|[0-9]{1,3}")
PARITY_INV="b290902a-b0de-4d40-aa10-47d44617bfd5"
PARITY="//pkg/sentry/devices/nvproxy:nvproxy_driver_parity_test"
PARITY_SHA="e5a2bcced9e94074191486ad5d0e4780e82756113af74b27691e6661a004fb74"
KEY=os.environ.pop("BUILDBUDDY_API_KEY")
ROOT=Path(os.environ["RUNNER_TEMP"])
REPORT={"invocation":IID,"console":{},"targets":{}}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None
opener=urllib.request.build_opener(NoRedirect)
def read(method,payload,limit):
    assert (method in ("GetEventLogChunk","GetTarget") and payload["invocationId"]==IID) or (
        method=="GetTarget" and payload=={"invocationId":PARITY_INV,"targetLabel":PARITY})
    req=urllib.request.Request("https://remote.buildbuddy.io/rpc/BuildBuddyService/"+method,
        data=json.dumps(payload).encode(),headers={"Content-Type":"application/json","x-buildbuddy-api-key":KEY})
    with opener.open(req,timeout=45) as res:
        data=res.read(limit+1)
    if len(data)>limit:raise ValueError("response limit")
    return json.loads(data)
def error(e):
    return getattr(e,"code",type(e).__name__)
def save():
    (ROOT/"closed-source-aborts.json").write_text(json.dumps(REPORT,separators=(",",":")))
def enum(v):
    return str(v) if v is not None and ENUM.fullmatch(str(v)) else None
def row(text,number):
    if KEY in text or re.search(r"(?i)authorization\s*[:=]|remote_header|x-buildbuddy-api-key|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}",text):
        return None
    labels=[l for l in TARGETS if l in text]
    strong=bool(re.search(r"^\s*(?:ERROR:|FAIL:)|analysis of target|not compatible|incompatible|no matching",text,re.I))
    if not labels and not strong:return None
    result={"line":number,"targets":labels,"signals":[w for w in WORDS if w.lower() in text.lower()]}
    # Only allow public tracked paths to leave this runner.
    loc=[]
    for f,n in re.findall(r"(?:^|[\s/])([A-Za-z0-9_.+/-]{1,170}):([0-9]{1,6})",text):
        parts=f.strip("/").split("/")
        if "." in parts or ".." in parts:continue
        for i in range(len(parts)):
            candidate="/".join(parts[i:])
            if candidate.startswith(("runsc/","pkg/","tools/","test/","vdso/")) and Path(candidate).is_file():
                loc.append([candidate,int(n)]);break
    if loc:result["locations"]=loc[:4]
    return result
for target in TARGETS:
    rec=REPORT["targets"][target]={"groups":[]}
    try:
        data=read("GetTarget",{"invocationId":IID,"targetLabel":target},1500000)
        for group in data.get("targetGroups",[]):
            for t in group.get("targets",[]):
                if t.get("metadata",{}).get("label")!=target:continue
                record={"target_status":enum(t.get("status")),"group_status":enum(group.get("status")),
                    "root_cause":t.get("rootCause",False) is True,
                    "test_event_count":len(t.get("testResultEvents",[])),"action_event_count":len(t.get("actionEvents",[]))}
                record["actions"]=[]
                for event in t.get("actionEvents",[])[:10]:
                    act=event.get("action",{}); uri=act.get("stderr",{}).get("uri","")
                    m=re.search(r"/blobs/([0-9a-f]{64})/([0-9]{1,13})$",uri)
                    record["actions"].append({"exit":int(act.get("exitCode",0)),"stderr":[m[1],int(m[2])] if m else None})
                rec["groups"].append(record)
        rec["fetched"]=True
    except (ValueError,KeyError,urllib.error.HTTPError,OSError) as exc:rec["error"]=error(exc)
    save()
try:
    cursor=""
    checked=set()
    details=REPORT["console"]; details["chunks"]=[]; details["markers"]=[]
    for page in range(6):
        payload={"invocationId":IID,"minLines":750}
        if cursor:payload["chunkId"]=cursor
        data=read("GetEventLogChunk",payload,6*1024**2)
        raw=base64.b64decode(data.get("buffer",""),validate=True)
        lines=re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",raw.decode(errors="replace")).splitlines()
        details["chunks"].append({"page":page,"bytes":len(raw),"lines":len(lines),"sha256":hashlib.sha256(raw).hexdigest()})
        hit=[r for i,line in enumerate(lines) if (r:=row(line,i+1))]
        # Reserve space for named-target lines even if compiler progress is noisy.
        for count,rows in [(24,(r for r in hit if r["targets"])),(16,(r for r in hit if not r["targets"]))]:
            for n,r in enumerate(rows):
                if n>=count:break
                details["markers"].append({"page":page,**r})
        cursor=data.get("previousChunkId","")
        if not cursor:break
        if cursor in checked or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}",cursor):
            raise ValueError("invalid previous chunk")
        checked.add(cursor)
    details["complete"]=not bool(cursor)
    details["fetched"]=True
except (ValueError,KeyError,urllib.error.HTTPError,OSError) as exc:REPORT["console"]["error"]=error(exc)
save()


report=REPORT["parity"]={"invocation":PARITY_INV,"target":PARITY,"sha256":PARITY_SHA}
try:
    meta=read("GetTarget",{"invocationId":PARITY_INV,"targetLabel":PARITY},1800000)
    sizes=set()
    for group in meta.get("targetGroups",[]):
        for target in group.get("targets",[]):
            if target.get("metadata",{}).get("label") != PARITY:continue
            for ev in target.get("testResultEvents",[]):
                for f in ev.get("testResult",{}).get("testActionOutput",[]):
                    if f.get("name") != "test.log":continue
                    m=re.fullmatch(r"bytestream://[^/]+/(?:[^?#]*/)?(?:blobs|compressed-blobs/zstd)/"+PARITY_SHA+r"/([0-9]{1,8})",f.get("uri",""))
                    if m:sizes.add(int(m[1]))
                    if f.get("digest")==PARITY_SHA and f.get("length"):sizes.add(int(f["length"]))
    report["size_matches"]=len(sizes)
    if len(sizes)!=1:raise ValueError("no exact original test log digest")
    n=sizes.pop()
    if not 1<=n<=5*1024**2:raise ValueError("outcome too large")
    url=f"https://remote.buildbuddy.io/file/download?invocation_id={PARITY_INV}&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{PARITY_SHA}%2F{n}"
    with opener.open(urllib.request.Request(url,headers={"x-buildbuddy-api-key":KEY}),timeout=30) as resp:
        raw=resp.read(n+1)
    assert len(raw)==n and hashlib.sha256(raw).hexdigest()==PARITY_SHA
    report.update(fetched=True,bytes=n)
    literal=["failed to run driver_ast_parser","failed to create parser runner","Failed to find driver_ast_parser",
        "failed to create temporary", "failed to get struct names","failed to clone","failed to checkout",
        "failed to download","failed to open","failed to get","failed to find","failed to parse","failed to read",
        "could not resolve host","could not resolve proxy","dial tcp","connection refused","context deadline exceeded",
        "no such host","network is unreachable","TLS","timeout","403 Forbidden","404 Not Found","502 Bad Gateway",
        "executable file not found","No such file or directory","not found in","not present","unknown flag","exit status",
        "driver checksum","checksum mismatch","computed checksum","unexpected checksum","hash mismatch","version mismatch",
        "download.nvidia.com","download.nvidia.cn","NVIDIA/open-gpu-kernel-modules","nvidia.com","github.com",
        "Skipping parity test","not available","has changed","different","not equal","expected","got","wanted","want",
        "git clone","git","curl","wget","python","clang","openat","permission denied","read-only file system",
        "couldn't connect","unsupported protocol scheme","transport","code=Unavailable","Bad Gateway","not found",
        "does not match","build tag","source directory","unexpected EOF","exec:","not executable","bad file descriptor"]
    unique={}
    for i,line in enumerate(raw.decode(errors="replace").splitlines(),1):
        if KEY in line or re.search(r"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}",line):continue
        locations=re.findall(r"(nvproxy_driver_parity_test\.go|auxiliary_files\.go|runner\.go|driver_ast_parser_test\.go):([0-9]{1,5})",line)
        matches=[s for s in literal if s.lower() in line.lower()]
        if not matches and not locations:continue
        key=(tuple(matches),tuple(locations))
        if key not in unique and len(unique)<45:
            unique[key]={"first_line":i,"signals":matches,"locations":[[f,int(no)] for f,no in locations],"count":0}
        if key in unique:unique[key]["count"]+=1
    report["diagnostics"]=list(unique.values())
except (ValueError,KeyError,urllib.error.HTTPError,OSError,AssertionError) as exc:
    report["error"]=error(exc)
save()
