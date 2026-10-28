"""Read the corrected run's original BEP, reports and capture before publishing."""
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import tarfile
from uuid import UUID
import xml.etree.ElementTree as ET
import zipfile

from wire import inspect_pcap, need

base = Path(__file__).resolve().parent
sha = lambda d: hashlib.sha256(d).hexdigest()
plan = json.loads((base/"plan.json").read_bytes())
receipt = json.loads((base/"receipt.json").read_bytes())
submission = json.loads((base/"submission.json").read_bytes())
need(plan["request_sha256"] == sha((base/"request.json").read_bytes()) == submission["request_sha256"], "request mismatch")
rows = {}
out = {"source":plan["executed_source"], "request":plan["request_sha256"],"invocation":str(UUID(submission["invocationId"])),
       "status":receipt.get("status"),"producer":receipt.get("producer_status"),"collection":receipt.get("collection_status"),"owners":rows}
bundle = base/"qualification.tar.gz"
if bundle.is_file():
    data=bundle.read_bytes();out["bundle"]={"sha256":sha(data),"bytes":len(data)}
    stamps=[]
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names=set(tar.getnames())
        need(len(names)==len(tar.getnames()),"duplicate original member")
        def get(name):
            entry=tar.getmember(name)
            need(entry.isfile() and 0<=entry.size<=64<<20,"invalid original member")
            return tar.extractfile(entry).read()
        prod=json.loads(get("dsack-corrected.json")) if "dsack-corrected.json" in names else {}
        if prod:
            need((prod["source"],prod["parent"],prod["tree"])==(out["source"],plan["parent"],plan["source_tree"]),"wrong source")
        for phase,labels in plan["phases"].items():
            prefix="invocation-"+phase+"/"
            if prefix+"events.jsonl" not in names:continue
            ctx=json.loads(get(prefix+"context.json"))
            need(ctx["expected_source"]==out["source"]==get(prefix+"head.txt").decode().strip(),"wrong checkout")
            stamps.append(datetime.fromisoformat(ctx["execution_finished_at"].replace("Z","+00:00")))
            exit_code=int(get(prefix+"native-exit"))
            raw=get(prefix+"events.jsonl");man=json.loads(get(prefix+"events.jsonl.manifest.json"))
            need((len(raw),sha(raw))==(man["events"]["bytes"],man["events"]["sha256"]),"bad BEP identity")
            events=[json.loads(x) for x in raw.splitlines()]
            [child]=[str(UUID(e["started"]["uuid"])) for e in events if e.get("started",{}).get("uuid")]
            for label in labels:
                row=rows[label]={"child":child,"native_exit":exit_code}
                src=prod.get("owners",{}).get(label,{})
                row["producer_verified"]=src.get("verified") is True
                row["worker_verified"]=src.get("worker_verified") is True
                row["behavior_verified"]=src.get("behavior_verified") is True
                found=[e for e in events if e.get("id",{}).get("testResult",{}).get("label")==label and "testResult" in e]
                if len(found)!=1:continue
                [e]=found;ident=e["id"]["testResult"];result=e["testResult"]
                need(all(ident[k]==1 for k in ("run","shard","attempt")),"unexpected attempts")
                row["status"]=result["status"]
                row["uncached_remote"]=result.get("executionInfo",{}).get("strategy")=="remote" and not result.get("cachedLocally") and not result.get("executionInfo",{}).get("cachedRemotely")
                captures=[]
                for r in man["reports"]:
                    if r["identity"]!=ident:continue
                    name=r["name"];path=r["path"];b=get(prefix+path)
                    need((len(b),sha(b))==(r["bytes"],r["sha256"]),"bad original report")
                    need(path.startswith("reports/") and "/bazel-out/" in path and ".." not in Path(path).parts,"bad original path")
                    rel="bazel-out/"+path.split("/bazel-out/",1)[1]
                    need(sum(v["name"]==name and v["uri"].endswith("/"+rel) for v in result["testActionOutput"])==1,"bad original URI")
                    if name in ("test.xml","test.log"):
                        need(name not in row,"duplicate primary")
                        row[name]={"sha256":sha(b),"bytes":len(b)}
                    if name=="test.xml":
                        cases=ET.fromstring(b).findall(".//testcase")
                        row["cases"]=len(cases)
                        row["failed"]=sum(any(n.find(t) is not None for t in ("error","failure")) for n in cases)
                        row["skipped"]=sum(n.find("skipped") is not None for n in cases)
                        names_in_xml={n.get("name") for n in cases}
                        row["named_wire"]=names_in_xml=={"TestDSACKAboveGap"}
                        row["receiver_controls"]=sorted(names_in_xml & {"TestReceiverDSACK/SACK=true","TestReceiverDSACK/SACK=false"})
                    if name=="test.log":
                        import re
                        row["markers"]=[a for a in ("amd64","arm64") if re.search(a+r"\s*\.\.\.\s*PASS",b.decode(errors="replace"),re.I)]
                    if name.endswith(".pcap"):captures.append(b)
                    if name=="test.outputs__outputs.zip":
                        with zipfile.ZipFile(io.BytesIO(b)) as z:
                            for f in z.infolist():
                                if f.filename.endswith(".pcap"):
                                    need(0<f.file_size<8<<20,"capture bound")
                                    captures.append(z.read(f))
                if phase=="packet-impact" and len(captures)==1:
                    w=inspect_pcap(captures[0])
                    row["pcap"]={"sha256":sha(captures[0]),"bytes":len(captures[0])}
                    row["observations"]=[{k:f[k] for k in ("sent","ack","blocks")} for f in w["frames"]]
                    row["matches_original_linux"]=row["observations"]==plan["linux_observations"]
                    row["matches_producer"]=w==src.get("wire")
    if stamps and receipt.get("consumer_verified_at"):
        end=max(stamps)
        out["native_finished_at"]=end.isoformat()
        out["native_finish_to_verified_s"]=(datetime.fromisoformat(receipt["consumer_verified_at"].replace("Z","+00:00"))-end).total_seconds()
(base/"outcome.json").write_text(json.dumps(out,indent=2)+"\n")
