"""Recover only the original reviewed Ubuntu capture image from its Bazel build."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
from urllib import error, request

work=Path(os.environ["RUNNER_TEMP"])/"tcpdump-reserve"
arch=os.environ["CPU"]
iid={"amd64":"af91d054-d8fd-40fb-b6b3-728e8827a745","arm64":"00f594b2-eff6-461a-a81d-e5b795e59454"}[arch]
archive=work/"transfer/image.tar"
label="//images/basic:tcpdump_context"
known="13726da574d970311d0dac89990243b91c4d0b5c51ab9a50efe5b4ef0fd055d7"
record={"invocation":iid,"label":label,"cpu":arch,"image_source":"77fbd4df0b319727914eec2f6112d724f06fed29","recovered":False}

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None


def call(api, message, maximum):
    assert api in ("GetAction","GetInvocation","GetFile")
    req=request.Request("https://app.buildbuddy.io/api/v1/"+api,data=json.dumps(message).encode(),
                        headers={"Content-Type":"application/json","x-buildbuddy-api-key":key})
    with opener.open(req,timeout=90) as response:
        data=response.read(maximum+1)
    if len(data)>maximum:
        raise ValueError("response limit")
    return data


def files(items):
    found=set()
    for f in items:
        if Path(f.get("name","")).name!="tcpdump_context.tar":
            continue
        m=re.fullmatch(r"bytestream://(?:cache|remote)\.buildbuddy\.io/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/([0-9]+)",f.get("uri",""))
        if m and 0<int(m[2])<=96*1024*1024:
            found.add((m[1],int(m[2])))
    return found


key=os.environ.pop("BUILDBUDDY_API_KEY")
assert key
opener=request.build_opener(NoRedirect)
try:
    selected=set()
    try:
        obj=json.loads(call("GetAction",{"selector":{"invocationId":iid,"targetLabel":label}},1024*1024))
        for act in obj.get("action",[]):
            if act.get("targetLabel",act.get("target_label"))==label and act.get("id",{}).get("invocationId",act.get("id",{}).get("invocation_id"))==iid:
                selected |= files(act.get("file",[]))
    except error.HTTPError as exc:
        record["action_status"]=exc.code
    if not selected:
        obj=json.loads(call("GetInvocation",{"selector":{"invocationId":iid},"includeArtifacts":True},4*1024*1024))
        for i in obj.get("invocation",[]):
            if i.get("id",{}).get("invocationId",i.get("id",{}).get("invocation_id"))==iid:
                selected |= files(i.get("artifacts",[]))
    if len(selected)!=1:
        raise LookupError("original context is absent or ambiguous")
    h,size=selected.pop()
    record.update(context_sha256=h,context_size=size)
    data=call("GetFile",{"uri":f"bytestream://remote.buildbuddy.io/blobs/{h}/{size}"},size)
    if len(data)!=size or hashlib.sha256(data).hexdigest()!=h:
        raise ValueError("content mismatch")
    with tarfile.open(fileobj=io.BytesIO(data),mode="r:*") as tar:
        members=[m for m in tar if m.isfile()]
        if len(members)!=1 or members[0].name.removeprefix("./")!="images/basic/tcpdump/image.tar" or not 0<members[0].size<=96*1024*1024:
            raise ValueError("unexpected image context")
        inner=tar.extractfile(members[0]).read(members[0].size)
    digest=hashlib.sha256(inner).hexdigest()
    if arch=="amd64" and digest!=known:
        raise ValueError("original AMD64 image hash")
    archive.write_bytes(inner)
    record.update(archive_sha256=digest,recovered=True)
except Exception as exc:
    record["reason"]=type(exc).__name__
    if isinstance(exc,error.HTTPError):
        record["http_status"]=exc.code
encoded=json.dumps(record,indent=2).encode()
assert len(encoded)<4096 and key.encode() not in encoded
(work/"original.json").write_bytes(encoded)
