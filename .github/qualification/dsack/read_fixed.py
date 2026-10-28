"""Project the original corrected receiver's native tests and captured TCP frames."""
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID
import io
import json
import os
import re
import tarfile
import traceback
import xml.etree.ElementTree as ET
import zipfile

from wire import inspect_pcap, isolate_flow

ORIGINAL = "489df4e7-8eda-4612-8bb4-5f84c30ca68c"
HOST = "cb548161f537465fda2576793e6d6cea59a32260"
SOURCE = "19b9ac70fff949d3bcf8104efd1ea5e08c3ecfa2"
ROOTS = {"TestReceiverDSACK","TestSackPermittedConnect","TestSackDisabledConnect","TestSackPermittedAccept","TestSackDisabledAccept","TestUpdateSACKBlocks","TestTrimSackBlockList","TestSACKRecovery","TestRecoveryEntry","TestDetectSpuriousRecoveryWithRTO","TestSACKDetectSpuriousRecoveryWithDupACK","TestNoSpuriousRecoveryWithDSACK"}
def identity(data):
    return {"sha256":sha256(data).hexdigest(),"bytes":len(data)}
def read(key, method, body, limit):
    assert method in ("GetInvocation","GetFile")
    c=HTTPSConnection("app.buildbuddy.io",timeout=50)
    try:
        c.request("POST","/api/v1/"+method,json.dumps(body),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
        with c.getresponse() as r:
            assert r.status==200
            b=r.read(limit+1);assert len(b)<=limit
            return b
    finally: c.close()
def read_archive(key):
    [inv]=json.loads(read(key,"GetInvocation",{"selector":{"invocation_id":ORIGINAL},"include_artifacts":True},8<<20))["invocation"]
    assert inv["id"]["invocationId"]==ORIGINAL and inv["commitSha"]==HOST
    [uri]=[a["uri"] for a in inv["artifacts"] if a.get("name")=="qualification.tar.gz"]
    u=urlsplit(uri)
    m=re.fullmatch(r".*/blobs/([a-f0-9]{64})/([1-9][0-9]*)",u.path)
    assert u.scheme=="bytestream" and u.netloc=="remote.buildbuddy.io" and m
    d,n=m.groups();n=int(n);assert n<=32<<20
    b=read(key,"GetFile",{"uri":uri},n)
    assert identity(b)=={"bytes":n,"sha256":d}
    return b
def observe(tar, out):
    plan=json.loads(Path("plan.json").read_bytes()); assert plan["executed_source"]==SOURCE
    ms=tar.getmembers(); assert len(ms)<=4096 and len(ms)==len({m.name for m in ms})
    def get(name):
        m=tar.getmember(name);assert m.isfile() and 0<=m.size<=64<<20
        return tar.extractfile(m).read()
    def obj(name):return json.loads(get(name))
    prod=obj("dsack-corrected.json");assert (prod["source"],prod["parent"],prod["tree"])==(SOURCE,plan["parent"],plan["source_tree"])
    status=obj("verdict.json")["status"];assert status in ("PASS","FAIL","INCOMPLETE")
    out["producer_status"]=status
    stamps=[]
    for phase, labels in plan["phases"].items():
        prefix="invocation-"+phase+"/"
        try:
            ctx=obj(prefix+"context.json")
            assert ctx["expected_source"]==SOURCE==get(prefix+"head.txt").decode().strip()
            finish=datetime.fromisoformat(ctx["execution_finished_at"].replace("Z","+00:00"));stamps.append(finish)
            raw=get(prefix+"events.jsonl");man=obj(prefix+"events.jsonl.manifest.json")
            assert identity(raw)=={k:man["events"][k] for k in ("bytes","sha256")}
            es=[json.loads(x) for x in raw.splitlines()]
            [child]=[str(UUID(e["started"]["uuid"])) for e in es if e.get("started",{}).get("uuid")]
            n=int(get(prefix+"native-exit"));assert 0<=n<256
        except Exception as exc:
            out["phase_errors"][phase]=failure(exc);continue
        for label in labels:
            row=out["owners"][label]={"child":child,"exit":n,"native_finished_at":finish.isoformat()}
            try:
                src=prod["owners"][label]
                row["original_verifier"]=src.get("verified") is True
                row["original_worker_join"]=src.get("worker_verified") is True
                row["original_behavior_gate"]=src.get("behavior_verified") is True
                row["producer_trace_lines"]=[int(i) for i in re.findall(r"/image_workload[.]py\", line ([0-9]+)",src.get("error",""))]
                [e]=[e for e in es if e.get("id",{}).get("testResult",{}).get("label")==label and "testResult" in e]
                ident=e["id"]["testResult"];assert all(ident[k]==1 for k in ("run","shard","attempt"))
                result=e["testResult"];info=result.get("executionInfo",{})
                native=result["status"];assert native in ("PASSED","FAILED","TIMEOUT","INCOMPLETE")
                row.update(status=native,uncached_remote=info.get("strategy")=="remote" and not info.get("cachedRemotely") and not result.get("cachedLocally"))
                reports=[r for r in man["reports"] if r["identity"]==ident]
                assert len(reports)==len({r["name"] for r in reports})
                bodies={};caps=[];census={"same_path":0,"decoded_path":0,"other_resolved_path":0,"bytestream_digest":0}
                for r in reports:
                    name=r["name"];p=r["path"];assert p.startswith("reports/") and ".." not in Path(p).parts
                    b=get(prefix+p);assert identity(b)=={k:r[k] for k in ("bytes","sha256")}
                    [v]=[v for v in result["testActionOutput"] if v["name"]==name]
                    u=urlsplit(v["uri"])
                    # capture() resolves the advertised URI before retaining its bytes.
                    # A bytestream URI authenticates content; its path names the digest,
                    # not the execroot. File URIs remain bound by exact attempt/name.
                    rel="bazel-out/"+p.split("/bazel-out/",1)[1]
                    if u.scheme=="bytestream":
                        assert u.netloc=="remote.buildbuddy.io" and u.path.endswith("/blobs/"+r["sha256"]+"/"+str(r["bytes"]));census["bytestream_digest"]+=1
                    else:
                        assert u.scheme=="file" and u.netloc in ("","localhost")
                        typ="same_path" if u.path.endswith("/"+rel) else "decoded_path" if unquote(u.path).endswith("/"+rel) else "other_resolved_path"
                        census[typ]+=1
                    if name in ("test.xml","test.log"):bodies[name]=b
                    if name.endswith(".pcap"):caps.append(b)
                    if name=="test.outputs__outputs.zip":
                        with zipfile.ZipFile(io.BytesIO(b)) as z:
                            for f in z.infolist():
                                if f.filename.endswith(".pcap"):
                                    assert 0<f.file_size<8<<20;caps.append(z.read(f))
                assert set(bodies)=={"test.log","test.xml"}
                row["uri_census"]=census;row["primary"]={k:identity(b) for k,b in bodies.items()}
                cases=ET.fromstring(bodies["test.xml"]).findall(".//testcase")
                ns={c.get("name") for c in cases};assert len(ns)==len(cases)
                row.update(cases=len(ns),failed=sum(any(c.find(t) is not None for t in ("failure","error")) for c in cases),skipped=sum(c.find("skipped") is not None for c in cases))
                log=bodies["test.log"].decode(errors="replace")
                if phase=="packet-impact":
                    assert ns=={"TestDSACKAboveGap"} and len(caps)==1
                    b=caps[0];row["pcap"]=identity(b)
                    try:w=inspect_pcap(b);row["unique_tuple_filter"]=False
                    except ValueError:w=inspect_pcap(isolate_flow(b));row["unique_tuple_filter"]=True
                    rows=[{k:f[k] for k in ("sent","ack","blocks")} for f in w["frames"]]
                    row["observations"]=rows;row["matches_original_linux"]=rows==plan["linux_observations"]
                    row["case_in_log"]="--- PASS: TestDSACKAboveGap" in log
                elif phase=="sack-suite":
                    row["source_roots_match"]={n for n in ns if "/" not in n}==ROOTS
                    row["receiver_controls"]=[x for x in ("TestReceiverDSACK/SACK=true","TestReceiverDSACK/SACK=false") if x in ns]
                else:row["markers"]=[a for a in ("amd64","arm64") if re.search(a+r"\s*\.\.\.\s*PASS",log,re.I)]
            except Exception as exc:row["diagnostic"]=failure(exc)
    if stamps:out["last_native_finish"]=max(stamps).isoformat()
def failure(exc):
    return {"type":type(exc).__name__,"frames":[x.lineno for x in traceback.extract_tb(exc.__traceback__) if Path(x.filename).name=="read_fixed.py"]}
def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY")
    out={"source":SOURCE,"original":ORIGINAL,"owners":{},"phase_errors":{}}
    try:
        b=read_archive(key);key=None;out["archive"]=identity(b)
        with tarfile.open(fileobj=io.BytesIO(b)) as t:observe(t,out)
    except Exception as exc:out["diagnostic"]=failure(exc)
    finally:
        key=None
        out["read_verified_at"]=datetime.now(timezone.utc).isoformat()
        Path("verdict.json").write_text(json.dumps(out,indent=2)+"\n")
    return 1 if out.get("diagnostic") or out["phase_errors"] or len(out["owners"])!=4 or any("diagnostic" in r for r in out["owners"].values()) else 0
if __name__=="__main__":raise SystemExit(main())
