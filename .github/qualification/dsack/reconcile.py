"""Reconcile the native reports and both original unmodified DUT captures."""
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

from wire import inspect_pcap, need

base = Path(__file__).resolve().parent
sha = lambda d: hashlib.sha256(d).hexdigest()
plan = json.loads((base/"plan.json").read_text())
receipt = json.loads((base/"receipt.json").read_text())
sub = json.loads((base/"submission.json").read_text())
need(plan["request_sha256"] == sub["request_sha256"] == sha((base/"request.json").read_bytes()),"different operation")
out = {"source":plan["executed_source"],"parent":plan["parent"],"tree":plan["source_tree"],"outer":sub["invocationId"],
       "status":receipt.get("status"),"producer_status":receipt.get("producer_status"),"collection_status":receipt.get("collection_status"),
       "verified_at":receipt.get("consumer_verified_at"),"owners":{}}
bundle = base/"qualification.tar.gz"
if not bundle.is_file():
    out["incomplete"] = "No original archive"
else:
    content = bundle.read_bytes()
    out["bundle"] = {"sha256":sha(content),"bytes":len(content)}
    with tarfile.open(fileobj=io.BytesIO(content)) as tar:
        names = tar.getnames()
        need(len(names) == len(set(names)), "duplicate archive members")
        out["bundle"]["members"] = len(names)
        get = lambda path: tar.extractfile(path).read()
        read = lambda path: json.loads(get(path))
        path = "invocation-packet-impact/"
        record = read("dsack-observations.json") if "dsack-observations.json" in names else {}
        out["source_guard"] = {k:record.get(k) for k in ("source","parent","tree")}
        if path+"context.json" not in names:
            out["incomplete"] = "No original PacketImpact child"
        else:
            ctx,verdict = read(path+"context.json"),read(path+"verdict.json")
            out["child"] = verdict["invocation_id"]
            out["native_exit"] = int(get(path+"native-exit").strip())
            out["native_finished_at"] = ctx["execution_finished_at"]
            for k in ("verified_at","finished_at"):
                if receipt.get(k):
                    out[k] = receipt[k]
            if out["verified_at"]:
                stamp = lambda s: datetime.fromisoformat(s.replace("Z","+00:00"))
                out["native_finish_to_verified_s"] = (stamp(out["verified_at"])-stamp(ctx["execution_finished_at"])).total_seconds()
            need(ctx["expected_source"] == out["source"] == get(path+"head.txt").decode().strip(),"wrong executed revision")
            ev = get(path+"events.jsonl")
            manifest = read(path+"events.jsonl.manifest.json")
            need((len(ev),sha(ev)) == (manifest["events"]["bytes"],manifest["events"]["sha256"]), "BEP digest")
            events = [json.loads(line) for line in ev.splitlines()]
            for label in plan["owners"]:
                original = [e for e in events if e.get("id",{}).get("testResult",{}).get("label") == label and "testResult" in e]
                row = out["owners"][label] = {}
                if len(original) != 1:
                    row["incomplete"] = "missing/duplicate original BEP"; continue
                e = original[0]
                row["status"] = e["testResult"]["status"]
                row["producer"] = record.get("owners",{}).get(label,{})
                reports = {r["name"]:r for r in manifest["reports"] if r["identity"] == e["id"]["testResult"]}
                captures = []
                for name,r in reports.items():
                    d = get(path+r["path"])
                    need((len(d),sha(d)) == (r["bytes"],r["sha256"]), "original report identity "+name)
                    if name == "test.xml":
                        nodes = ET.fromstring(d).findall(".//testcase")
                        row["xml"] = r
                        row["cases"] = [{"name":n.get("name"),"errors":[{"tag":v.tag,"message":v.get("message"),"text":"".join(v.itertext())} for v in n if v.tag in ("failure","error","skipped")]} for n in nodes]
                    elif name == "test.log":
                        row["log"] = r
                    elif name.endswith(".pcap"):
                        captures.append((name,d))
                    elif name == "test.outputs__outputs.zip":
                        with zipfile.ZipFile(io.BytesIO(d)) as z:
                            for i in z.infolist():
                                if i.filename.endswith(".pcap"):
                                    need(i.file_size <= 8 << 20,"pcap bound")
                                    captures.append((i.filename,z.read(i)))
                if len(captures) == 1:
                    name,d = captures[0]
                    capture = inspect_pcap(d)
                    row["wire"] = capture | {"name":name}
                    need(not row["producer"].get("wire") or row["producer"]["wire"] == row["wire"],"original pcap differs from producer")
                else:
                    row["incomplete"] = "missing or multiple capture files"
out["scope"] = "One negotiated IPv4 TCP stream, matching test on native Linux and gVisor on the reviewed #16403 receiver. The producer can retain a failing native DUT and must not be treated as its passing result."
(base/"outcome.json").write_text(json.dumps(out,indent=2)+"\n")
print(json.dumps({k:out.get(k) for k in ("outer","status","child","bundle","native_finish_to_verified_s")}))
