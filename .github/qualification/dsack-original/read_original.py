"""Read only the exact completed D-SACK archive and project selected evidence."""
from hashlib import sha256
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit
import io
import json
import os
import re
import tarfile
import xml.etree.ElementTree as ET

ID="661fdb89-e442-4ac4-90cf-c1423462fbd6"
CHILD="953a668c-cd3a-43b1-b030-0670bbef843f"
SOURCE="924c3e9f13ff3f7e7c888d3d70043076dfd7740e"
HOST="cb548161f537465fda2576793e6d6cea59a32260"
DIGEST="d1e0fcf2b83bbd6324696d6f3a1f9ef6fd857a4726ce409f1483f3f89bf5b275"
SIZE=915352
IMAGE="docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
OWNERS=["//test/packetimpact/tests:tcp_rack_"+s+"_test" for s in ("native","netstack")]
CASE="TestDSACKAboveGap"
SHA=lambda b:sha256(b).hexdigest()
def check(b):
    return {"bytes":len(b),"sha256":SHA(b)}

def read(key,method,body,limit):
    assert method in ("GetInvocation","GetFile")
    connection=HTTPSConnection("app.buildbuddy.io",timeout=30)
    try:
        connection.request("POST","/api/v1/"+method,json.dumps(body),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
        with connection.getresponse() as response:
            assert response.status==200
            data=response.read(limit+1);assert len(data)<=limit
            return data
    finally:connection.close()

def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY")
    out={"original":ID,"source":SOURCE,"child":CHILD,"owners":{}}
    try:
        [inv]=json.loads(read(key,"GetInvocation",{"selector":{"invocation_id":ID},"include_artifacts":True},4<<20))["invocation"]
        assert inv["id"]["invocationId"]==ID and inv["commitSha"]==HOST
        [uri]=[a["uri"] for a in inv.get("artifacts",[]) if a.get("name")=="qualification.tar.gz"]
        parsed=urlsplit(uri)
        assert parsed.scheme=="bytestream" and parsed.hostname=="remote.buildbuddy.io" and parsed.path.endswith("/blobs/"+DIGEST+"/"+str(SIZE))
        raw=read(key,"GetFile",{"uri":uri},SIZE);key=None
        assert len(raw)==SIZE and SHA(raw)==DIGEST
        out["archive"]=check(raw)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            names=tar.getnames();assert len(names)==len(set(names))
            def get(n):
                m=tar.getmember(n);assert m.isfile() and 0<=m.size<=16<<20
                return tar.extractfile(m).read()
            p="invocation-packet-impact/"
            ctx=json.loads(get(p+"context.json"))
            assert ctx["expected_source"]==SOURCE and get(p+"head.txt").decode().strip()==SOURCE
            out["native_finished_at"]=ctx["execution_finished_at"]
            man=json.loads(get(p+"events.jsonl.manifest.json"))
            ev=get(p+"events.jsonl");assert check(ev)=={k:man["events"][k] for k in ("bytes","sha256")}
            es=[json.loads(x) for x in ev.splitlines()]
            assert [e["started"]["uuid"] for e in es if e.get("started",{}).get("uuid")]==[CHILD]
            obs=json.loads(get("dsack-observations.json"));assert obs["source"]==SOURCE
            for label in OWNERS:
                [e]=[e for e in es if e.get("id",{}).get("testResult",{}).get("label")==label and "testResult" in e]
                original=obs["owners"][label];reason=original.get("error","")
                row=out["owners"][label]={"bep":e["testResult"]["status"],"strict":original.get("verified") is True,"reason":check(reason.encode()),"frames":[[s,int(n)] for s,n in re.findall(r"/(image_workload|dsack_wire)[.]py\", line (\d+)",reason)],"errors":[m for m in ("wrong TestRunner census","non-fresh BEP","worker mismatch","no physical expected output","BEP data","original primary/pcap names","wrong or skipped XML case","one controlled handshake","report path","manifest data","original test log","native exit/XML/BEP disagreement","unexpected attempt") if m in reason],"workers":[]}
                req=man["reports"];refs=[r for r in req if r["identity"]==e["id"]["testResult"]]
                files={r["name"]:get(p+r["path"]) for r in refs}
                for r in refs:assert check(files[r["name"]])=={k:r[k] for k in ("bytes","sha256")}
                info=e["testResult"].get("executionInfo",{})
                row["bep_strategy_remote"]=info.get("strategy")=="remote"
                row["fresh_bep"]=not info.get("cachedRemotely") and not e["testResult"].get("cachedLocally")
                row["identity"]={k:v for k,v in e["id"]["testResult"].items() if k in ("run","shard","attempt") and isinstance(v,int)}
                src=files["test.log"].decode(errors="replace")
                expected_errors=[line for line in src.splitlines() if re.search(r"[.]go:\d+: ",line)]
                row["diagnostics"]={"code_location_lines":len(expected_errors),"exact_ack_mismatch":sum("ACK at offset 0 has SACK blocks" in line for line in expected_errors),"recv_errors":sum(any(s in line for s in ("recv =","recv after stream =")) for line in expected_errors),"other":sum("ACK at offset 0 has SACK blocks" not in line for line in expected_errors)}
                [node]=ET.fromstring(files["test.xml"]).findall(".//testcase");assert node.get("name")==CASE
                row["xml"]=check(files["test.xml"]);row["log"]=check(files["test.log"]);row["xml_failed"]=node.find("failure") is not None
                row["primary_prefixes"]=[{"kind":r["name"],"execroot":r["path"].startswith("reports/execroot/_main/bazel-out/"),"bazelout":"/bazel-out/" in r["path"]} for r in refs if r["name"] in ("test.xml","test.log")]
                for a in original.get("observed_spawns",[]):
                    props={x["name"]:x["value"] for x in a.get("platform",{}).get("properties",[])}
                    worker={"test_setup":a.get("executable","").endswith("/test-setup.sh"),"remote":a.get("runner")=="remote","cached":a.get("cacheHit") is True,"exit":a.get("exitCode"),"linux":props.get("OSFamily")=="linux","amd64":props.get("Arch")=="amd64","firecracker":props.get("workload-isolation-type")=="firecracker","image_match":props.get("container-image")==IMAGE,"ipv6":props.get("network-enable-ipv6")=="true","action_output_count":len(a.get("actualOutputs",[])),"matched_reports":[]}
                    for output in a.get("actualOutputs",[]):
                        for r in refs:
                            rel=r["path"].split("/bazel-out/",1)[-1]
                            dep=output.get("digest",{})
                            if output.get("path")=="bazel-out/"+rel and dep.get("hash")==r["sha256"] and int(dep.get("sizeBytes","-1"))==r["bytes"]:
                                worker["matched_reports"].append(next((s for s in ("test.log","test.xml","test.outputs__outputs.zip") if r["name"]==s),"other"))
                    row["workers"].append(worker)
    except Exception as exc:
        import traceback
        out["error"]=type(exc).__name__;out["line"]=[f.lineno for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).name=="read_original.py"][-1]
    finally:
        key=None
        (Path(os.environ["RUNNER_TEMP"])/"dsack-original"/"verdict.json").write_text(json.dumps(out,indent=2)+"\n")
    return 1 if "error" in out or len(out["owners"])!=2 else 0

if __name__=="__main__":raise SystemExit(main())
