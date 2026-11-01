"""Read the original AMD64 link action and its binary, without running Bazel."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import json
import os
import re
import subprocess
import zipfile

tmp = Path(os.environ["RUNNER_TEMP"])
key = os.environ["BUILDBUDDY_API_KEY"]
original = tmp / "original.zip"
assert sha256(original.read_bytes()).hexdigest() == "ff6ffb470f45eb2ea9a941a373a56c368bb7b8d1a6bea253a100e9288f43a97d"
with zipfile.ZipFile(original) as z:
    receipt = json.loads(z.read("result.json"))
assert receipt["arm64"]["verdict"] == "RUNSC_BUILT"
arm = receipt["amd64"]
inv, = arm["invocations"]
assert inv == "d89b31d7-94de-4dc3-af33-f37bd13f491d" and arm["native_exit"] == 0
report = {"source": arm["source"], "original": receipt, "started": datetime.now(timezone.utc).isoformat(), "queries": []}
image = receipt["worker_image"]
label = "//runsc:runsc"
def output_path(path):
    return bool(re.fullmatch(r"bazel-out/k8-fastbuild(?:-[^/]*)?/bin/runsc/runsc_/runsc",path))
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
def read(url, data=None):
    request = Request(url, data, {"x-buildbuddy-api-key":key, "Content-Type":"application/json"})
    with build_opener(NoRedirect).open(request, timeout=65) as response:
        return response.read()
def snake(key):
    return re.sub(r"(?<!^)(?=[A-Z])","_",key).lower()
def rpc(route, body):
    return json.loads(read("https://app.buildbuddy.io/"+route,json.dumps(body).encode()),
                      object_pairs_hook=lambda items:{snake(k):v for k,v in items})
def blob(uri, name):
    assert re.fullmatch(r"bytestream://remote\.buildbuddy\.io/(?:[^?]*/)?blobs/[0-9a-f]{64}/\d+",uri)
    h,n=uri.split("/")[-2:]
    data=read("https://app.buildbuddy.io/file/download?"+urlencode({"invocation_id":inv,"bytestream_url":uri,"filename":name}))
    assert len(data)==int(n) and sha256(data).hexdigest()==h
    return data
def uri(digest):
    return "bytestream://remote.buildbuddy.io/blobs/"+digest["hash"]+"/"+str(digest.get("sizeBytes",digest.get("size_bytes")))
def download(kind,digest):
    ref=uri(digest).split("blobs/",1)[1]
    result=subprocess.run([str(tmp/"bb"),"download","--target=grpcs://remote.buildbuddy.io","--api_key="+key,"--type="+kind,ref],capture_output=True,timeout=45)
    if result.returncode: raise ValueError("CAS "+kind+" reader returned "+str(result.returncode))
    return json.loads(result.stdout)
def execution():
    data=rpc("rpc/BuildBuddyService/GetExecution",{"executionLookup":{"invocationId":inv,"targetLabel":label},"inlineExecuteResponse":True})
    found=data.get("execution",[])
    report["matching_executions"]=len(found)
    for entry in found:
        if entry.get("target_label")!=label or entry.get("action_mnemonic")!="GoLink": continue
        digest=entry["action_digest"]
        action=download("Action",digest)
        command=download("Command",action["commandDigest"])
        props={p["name"]:p["value"] for p in action.get("platform",command.get("platform",{})).get("properties",[])}
        assert props["Arch"]=="amd64" and props["container-image"]==image
        assert not entry.get("status",{}).get("code") and not entry.get("exit_code")
        response=entry["execute_response"]
        outputs=response["result"]["output_files"]
        output,=[o for o in outputs if output_path(o["path"])]
        return dict(owner=label, mnemonic="GoLink", runner="remote", action=digest, executable=command["arguments"][0], platform=props,
                    cache_hit=response.get("cached_result",False),output=output)
    raise ValueError("no completed //runsc:runsc GoLink from GetExecution")
def compact():
    data=rpc("api/v1/GetInvocation",{"selector":{"invocationId":inv},"includeBuildToolLogs":True})
    row,=data["invocation"]
    logs=row.get("build_tool_logs",[])
    report["available_logs"]=[Path(log["name"]).name for log in logs]
    log,=[log for log in logs if "execution" in log["name"] and log.get("uri","").startswith("bytestream:")]
    file=tmp/"original-execution.binpb";file.write_bytes(blob(log["uri"],"execution.binpb"))
    selector='select(.targetLabel=="//runsc:runsc" and .mnemonic=="GoLink")|{runner,cacheHit,app:.commandArgs[0],digest,platform,actualOutputs}'
    with subprocess.Popen([str(tmp/"bb"),"print","--compact_execution_log="+str(file)],stdout=subprocess.PIPE) as proc:
        reduced=subprocess.check_output(["jq","-c",selector],stdin=proc.stdout)
        proc.stdout.close()
        assert proc.wait()==0
    for line in reduced.splitlines():
        row=json.loads(line)
        for output in row["actualOutputs"]:
            if not output_path(output["path"]):continue
            props={p["name"]:p["value"] for p in row["platform"]["properties"]}
            assert props["Arch"]=="amd64" and props["container-image"]==image and (row["runner"]=="remote" or row["cacheHit"])
            return dict(owner=label,mnemonic="GoLink",runner=row["runner"],cache_hit=row["cacheHit"],executable=row["app"],platform=props,action=row["digest"],output=output)
    raise ValueError("no matching GoLink output in original compact spawn log")
def note_error(exc):
    return type(exc).__name__ + (": "+str(exc)[:160] if isinstance(exc,(KeyError,ValueError,StopIteration,AssertionError)) else " HTTP "+str(exc.code) if hasattr(exc,"code") else "")
try:
    for method in (execution,compact):
        try:
            record=method()
            report["owner_action"]=record
            data=blob(uri(record["output"]["digest"]),"runsc")
            assert data[:6]==b"\x7fELF\x02\x01" and int.from_bytes(data[18:20],"little")==62
            report.update(verdict="BOTH_ORIGINAL_RUNSC_ELFS_VERIFIED",original_binary=dict(elf_machine=62,bytes=len(data),sha256=sha256(data).hexdigest(),path=record["output"]["path"]))
            break
        except Exception as e: report["queries"].append([method.__name__,note_error(e)])
    else: report["verdict"]="INCOMPLETE"
finally:
    report["verified"]=datetime.now(timezone.utc).isoformat()
    (tmp/"runsc-verified.json").write_text(json.dumps(report,indent=2)+"\n")
    print(report['verdict'],report["queries"],flush=True)
if report["verdict"]=="INCOMPLETE":raise SystemExit(1)
