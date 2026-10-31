"""Recover only the original, content-addressed outputs of four package actions."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import status_cas as cas
from original_worker import rpc

cases = {
    "staged-amd64": ("8863f3b5-682e-4958-a573-c84600b7c7ff", "BUILD_EMBED_LABEL", "release-20991231.7"),
    "nightly-amd64": ("73364517-f427-477e-b2a3-9e198e3be0e9", "STABLE_VERSION", "dev-20981231.4"),
    "staged-arm64": ("375a4fe9-11d7-4e04-b312-776d7bf486fa", "BUILD_EMBED_LABEL", "release-20991231.7"),
    "retag-amd64": ("afe6360b-fffd-4659-8ce9-c20cc3f95457", "BUILD_EMBED_LABEL", "release-20991231.8"),
}
label = "//tools/bazeldefs:version"
root = Path(os.environ["RUNNER_TEMP"]) / "rack-results"
root.mkdir(exist_ok=True)

def file_at(digest, path):
    components = path.split("/")
    for i, name in enumerate(components):
        children = cas.fields(cas.blob(digest))
        kind = 1 if i == len(components) - 1 else 2
        refs = [dict(cas.fields(raw)) for number,raw in children if number == kind]
        child, = [ref for ref in refs if ref[1].decode() == name]
        digest = cas.digest_from(child[2])
    return digest

def event_output(inv):
    data=json.dumps({"selector":{"invocation_id":inv,"target_label":label}}).encode()
    request=Request("https://app.buildbuddy.io/api/v1/GetAction",data,{"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    with urlopen(request,timeout=30) as reply:
        raw=reply.read(2*1024*1024+1)
    assert len(raw)<=2*1024*1024
    rows=json.loads(raw).get("action",[])
    urls={f["uri"] for a in rows if a.get("targetLabel",a.get("target_label"))==label for f in a.get("file",[]) if Path(f["name"]).name=="version.txt"}
    url,=map(urlsplit,urls)
    m=re.fullmatch(r"/blobs/([0-9a-f]{64})/(\d+)",url.path)
    assert url.scheme=="bytestream" and url.netloc=="remote.buildbuddy.io" and m and not url.query and not url.fragment
    return {"hash":m.group(1),"sizeBytes":m.group(2)}

def recover(name, inv, key, value, row):
    cas.inv=inv
    attempts=[]
    for target in [label,"@@"+label,None]:
        lookup={"invocationId":inv}
        if target: lookup["targetLabel"]=target
        reply=rpc("GetExecution",{"executionLookup":lookup,"inlineExecuteResponse":True})
        attempts=[r for r in reply.get("execution",[]) if r.get("actionMnemonic")=="PackageVersion" and r.get("targetLabel","").removeprefix("@@")==label]
        if attempts: break
    assert len(attempts)==1, ("matching executions",len(attempts))
    ex,=attempts
    digest=ex["actionDigest"]
    row.update(action_digest=digest,exit=ex.get("exitCode",0),rpc_status=ex.get("status",{}).get("code",0))
    assert row["exit"]==0 and row["rpc_status"]==0
    action=dict(cas.fields(cas.blob(digest)))
    command=cas.fields(cas.blob(cas.digest_from(action[1])))
    args=[v.decode() for k,v in command if k==1]
    props=cas.selected_platform(action.get(10,dict(command).get(5,b"")))
    row.update(argv=args,platform=props)
    assert len(args)==3 and args[0].endswith("/package_version") and args[1]==key and args[2].endswith("/version.txt"),args
    assert props["container-image"]=="docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef" and props["Arch"]=="amd64",props
    env={p[1].decode():p[2].decode() for k,v in command if k==2 for p in [dict(cas.fields(v))] if p[1].decode() in ("BAZEL_STABLE_STATUS_FILE","BAZEL_VOLATILE_STATUS_FILE")}
    inputroot=cas.digest_from(action[2])
    row["declared_status_files"]={k:{"path":p,"digest":file_at(inputroot,p)} for k,p in env.items()}
    stable=row["declared_status_files"]["BAZEL_STABLE_STATUS_FILE"]["digest"]
    values=[s[len(key)+1:] for s in cas.blob(stable).decode().splitlines() if s.startswith(key+" ")]
    assert values==[value],values
    row["selected_input"]=values[0]
    response=ex.get("executeResponse",{})
    result=response.get("result",{})
    outputs=[f["digest"] for f in result.get("outputFiles",[]) if f["path"]==args[2]]
    if not outputs:
        detailed=rpc("GetExecution",{"executionLookup":{"invocationId":inv,"actionDigestHash":digest["hash"]},"inlineExecuteResponse":True})
        exact=[r for r in detailed.get("execution",[]) if r.get("actionDigest",{}).get("hash")==digest["hash"]]
        if len(exact)==1:
            response=exact[0].get("executeResponse",{})
            result=response.get("result",{})
            outputs=[f["digest"] for f in result.get("outputFiles",[]) if f["path"]==args[2]]
    output=outputs[0] if len(outputs)==1 else event_output(inv)
    assert int(output["sizeBytes"])<100
    content=cas.blob(output)
    assert content==(value.split("-",1)[1]+"\n").encode(),content
    row.update(output=output,package_version=content.decode().rstrip(),cached_result=response.get("cachedResult",False),verified=True)
    metrics=result.get("executionMetadata",ex.get("executedActionMetadata",{}))
    row['execution_times']={k:v for k,v in metrics.items() if k in ('executionStartTimestamp','executionCompletedTimestamp','workerStartTimestamp','workerCompletedTimestamp')}
    print(name,row["selected_input"],row["package_version"],row["output"]["hash"])

results={}
for name,(inv,key,value) in cases.items():
    row=results[name]={"invocation":inv,"source":"8529bfdd3776d087b9d705df0c5232a8337113ab","target":label}
    try: recover(name,inv,key,value,row)
    except Exception as exc:
        row["error"]=cas.safe(f"{type(exc).__name__}: {exc}")
    finally: (root/(name+".json")).write_text(json.dumps(row,indent=2)+"\n")
assert all(r.get("verified") for r in results.values()),{n:r.get("error") for n,r in results.items()}
assert results["staged-amd64"]["output"]==results["staged-arm64"]["output"]
assert results["staged-amd64"]["action_digest"]!=results["retag-amd64"]["action_digest"]
(root/"verdict").write_text("Verified all four original positive outputs, declared status inputs and scratch worker actions.\n")
