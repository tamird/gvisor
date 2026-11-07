"""Read only the two original failed invocations and the known linked ARM ELF."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request
import exec_cas as cas

S="097b8d31f4586709d20cf00abb75e8afabb07708"
STOCK="271e1346-2ebc-44fe-9e14-fecf2dea1070"
MAPPED="f6e19d40-4a89-48d9-83c5-0a1a0a66936e"
ACTION="35f8a0a33f66f0cecd524395a8994780e60c33efcceaeb3d564b016898288ae9"
ELF="7e34fe3c975a09c00f3486e7c9a447493b142e7aa02f989e4222d6cb39e7077a"
OUT="bazel-out/aarch64-fastbuild-ST-bd8908022ec6/bin/vdso/vdso.so"
W=Path(os.environ["RUNNER_TEMP"])/"original-arm-vdso"
result={"fixture":S,"link":ACTION,"invocations":{}}
def save():
    text=json.dumps(result,sort_keys=True,indent=1)
    assert len(text)<64000 and os.environ["ACCESS"] not in text
    (W/"original.json").write_text(text+"\n")
def elf():
    cas.INV=STOCK
    for e in cas.executions("//vdso:vdso.so","CppLink"):
        if e.get("actionDigest",{}).get("hash")!=ACTION:continue
        resp=e.get("executeResponse",{});a=resp.get("result",{})
        if resp.get("status",{}).get("code") or a.get("exitCode"):continue
        for f in a.get("outputFiles",[]):
            d=f.get("digest",{})
            if f.get("path")==OUT and d.get("hash")==ELF and 0<int(d["sizeBytes"])<=131072:
                b=cas.blob(d,131072); assert b[:6]==b"\x7fELF\x02\x01" and int.from_bytes(b[16:18],"little")==3 and int.from_bytes(b[18:20],"little")==183
                (W/"original.elf").write_bytes(b)
                result["elf"]={"sha256":hashlib.sha256(b).hexdigest(),"size":len(b),"machine":183,"kind":3}
                save();return
    raise ValueError("original link output missing")

def original_diagnostic(inv):
    row=result["invocations"][inv]={"chunks":[],"messages":[]}
    cursor="";prior=""
    for page in range(5):
        assert inv in (STOCK,MAPPED) and re.fullmatch(r"[A-Za-z0-9_-]{0,32}",cursor)
        body=json.dumps({"invocationId":inv,"chunkId":cursor,"minLines":125}).encode()
        req=urllib.request.Request("https://remote.buildbuddy.io/rpc/BuildBuddyService/GetEventLogChunk",data=body,headers={"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["ACCESS"]})
        with cas.opener.open(req,timeout=18) as response:buf=response.read(3*1024*1024+1)
        assert len(buf)<=3*1024*1024
        msg=json.loads(buf);raw=base64.b64decode(msg.get("buffer",""),validate=True)
        row["chunks"].append([len(raw),hashlib.sha256(raw).hexdigest()])
        text=raw.decode(errors="replace")+prior
        text=re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",text)
        lines=text.splitlines()
        markers=("error:","no matching","no registered","no compatible","exec format","failed_precondition","failed to execute","failed to build","not compatible","worker unavailable","failed to fetch","no such file")
        secret=re.compile(r"(?i)(remote_header|remote_cache_header|authorization|api[_-]key|bearer |cookie|github[_-]token|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_|credential|private.key)")
        for i,line in enumerate(lines):
            if not any(s in line.lower() for s in markers):continue
            for t in lines[i:i+2]:
                if secret.search(t) or os.environ["ACCESS"] in t:continue
                t=re.sub(r"https?://[^\s]*","<url>",t)
                t=re.sub(r"/(?:home/runner|root|tmp)/[^\s:]*?/external/","external/",t)
                t=re.sub(r"/(?:home/runner|tmp)/[^\s:]+","<runner file>",t)
                t=" ".join(t.strip().split())[:750]
                if t and t not in row["messages"] and len(row["messages"])<20:row["messages"].append(t)
        save()
        cursor=msg.get("previousChunkId","");prior=text[:700]
        if row["messages"] or not cursor:break
try:elf()
except Exception as exc:result["elf_error"]=type(exc).__name__;save()
for inv in (STOCK,MAPPED):
    try:original_diagnostic(inv)
    except Exception as exc:result["invocations"].setdefault(inv,{})["error"]=type(exc).__name__;save()
save()
