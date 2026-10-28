"""Read only the named original D-SACK invocation; publish bounded evidence."""
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPSConnection
from pathlib import Path
from uuid import UUID
from urllib.parse import urlsplit
import io
import json
import os
import re
import sys
import struct
import tarfile
import traceback
import zipfile

ID = "366dcd1a-941e-460f-a5e7-4f9025cbc14e"
SOURCE = "924c3e9f13ff3f7e7c888d3d70043076dfd7740e"
HOST = "cb548161f537465fda2576793e6d6cea59a32260"
OWNERS = tuple("//test/packetimpact/tests:tcp_rack_" + mode + "_test" for mode in ("native","netstack"))
CASE = "TestDSACKAboveGap"
KNOWN = ("original controlled sends including FIN", "one controlled handshake", "missing cumulative ACK", "expected Ethernet pcap", "pcap size",
    "wrong or skipped XML case", "no physical expected output", "original primary/pcap names", "source set", "Producer sources",
    "Expected exactly one qualification.tar.gz", "permission denied", "no space left on device", "no such package", "no such target",
    "not visible from", "violates visibility", "visibility-declared", "no such file or directory", "operation not permitted", "buildifier",
    "undefined:", "missing strict dependencies", "analysis of target", "cannot find", "cannot use", "no tests found", "test timed out",
    "exec format error", "Traceback (most recent call last)", "checksum mismatch", "fatal:", "unexpected EOF", "netns", "tcpdump", "CAP_NET_ADMIN")


def ident(x):
    return {"bytes":len(x),"sha256":sha256(x).hexdigest()}


def typed(s):
    return {"bytes":len(s.encode()), "sha256":sha256(s.encode()).hexdigest(),
            "kinds":[k for k in KNOWN if k.lower() in s.lower()],
            "error_labels":sorted(set(label for line in s.splitlines() if re.search(r"error|visibility|not visible|missing|no such|failed to build",line,re.I) for label in re.findall(r"(?<![/:])//(?:pkg|runsc|test|tools)/[a-zA-Z0-9_./-]*:[a-zA-Z0-9_.+-]+",line)))[:20],
            "frames":sorted(set((n,int(line)) for n,line in re.findall(r'/(image_workload|wire|dsack_wire|image_bootstrap|image_execution|image_qualification|qualification_report|read_original|reconcile)[.]py", line (\d+)',s)))[:24]}


def trace(exc):
    return {"class":type(exc).__name__,"lines":[(Path(x.filename).name,x.lineno) for x in traceback.extract_tb(exc.__traceback__) if Path(x.filename).name in ("read_original.py","wire.py","qualification_report.py")],
            "reason":typed(str(exc))}


def isolate_flow(raw):
    """Keep original records for the unique tuple carrying our eight test bytes."""
    ends={b"\xd4\xc3\xb2\xa1":"<",b"\xa1\xb2\xc3\xd4":">",b"\x4d\x3c\xb2\xa1":"<",b"\xa1\xb2\x3c\x4d":">"}
    endian=ends[raw[:4]]
    assert len(raw)>=24 and struct.unpack_from(endian+"I",raw,20)[0]==1
    records=[];payloads=set();i=24
    while i<len(raw):
        begin=i;assert i+16<=len(raw)
        cap=struct.unpack_from(endian+"I",raw,i+8)[0];i+=16
        assert i+cap<=len(raw)
        f=raw[i:i+cap];i+=cap
        if len(f)<54 or f[12:14]!=b"\x08\x00" or f[23]!=6:continue
        tcp_start=14+(f[14]&15)*4
        if tcp_start+20>len(f):continue
        sp,dp=struct.unpack_from("!HH",f,tcp_start)
        endpoint=((f[26:30],sp),(f[30:34],dp))
        pay=tcp_start+(f[tcp_start+12]>>4)*4
        if f[pay:14+struct.unpack_from("!H",f,16)[0]]==b"ijklmnop":payloads.add(endpoint)
        records.append((endpoint,raw[begin:i]))
    if len(payloads)!=1:raise ValueError("no unique controlled tuple")
    [peer]=payloads
    return raw[:24]+b"".join(record for ends,record in records if ends==peer or ends==peer[::-1])


def main():
    from xml.etree import ElementTree as ET
    root=Path(os.environ['RUNNER_TEMP'])/'dsack-original'
    sys.path.insert(0,str(root))
    import qualification_report as qr
    from wire import inspect_pcap
    key=os.environ.pop("BUILDBUDDY_API_KEY")
    public={"original":ID,"source":SOURCE,"owners":{}}
    def get(method,body,limit):
        assert method in ("GetInvocation","GetLog")
        c=HTTPSConnection("app.buildbuddy.io", timeout=25)
        try:
            c.request("POST","/api/v1/"+method,json.dumps(body),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
            with c.getresponse() as r:
                assert r.status==200
                b=r.read(limit+1);assert len(b)<=limit
                return json.loads(b)
        finally: c.close()
    class K:
        def read_text(self): return key
    try:
        ar=root/"qualification.tar.gz"
        [inv]=get("GetInvocation",{"selector":{"invocation_id":ID},"include_artifacts":True},4<<20)["invocation"]
        assert inv["id"]["invocationId"]==ID and inv["commitSha"]==HOST
        uris=[a["uri"] for a in inv.get("artifacts",[]) if a.get("name")=="qualification.tar.gz"]
        public["artifact_count"]=len(uris)
        assert len(uris)<=1 and all(urlsplit(u).scheme=="bytestream" and urlsplit(u).hostname=="remote.buildbuddy.io" and re.search(r"/blobs/[0-9a-f]{64}/([0-9]+)$",urlsplit(u).path) and int(u.rsplit("/",1)[1])<=32<<20 for u in uris)
        rec=qr.native_receipt(ID,K(),ar,root/"receipt.json",SOURCE,list(OWNERS),160,expected_sources=[SOURCE],hosted_source=HOST)
        public["receipt"]={k:rec.get(k) for k in ("status","producer_status","collection_status","downloaded_bytes","consumer_verified_at")}
        public["receipt"]["reason"]=typed(rec.get("collection_error",""))
        if ar.exists():
            raw=ar.read_bytes()
            assert len(uris)==1 and len(raw)<=32<<20 and uris[0].endswith("/blobs/"+sha256(raw).hexdigest()+"/"+str(len(raw)))
            public["archive"]=ident(raw)
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                names=tar.getnames();assert len(names)==len(set(names))
                def data(name):
                    m=tar.getmember(name);assert m.isfile() and 0<=m.size<=24<<20
                    return tar.extractfile(m).read()
                def obj(name):return json.loads(data(name))
                public["stages"]={}
                for ph in ("00-buildifier","dependency-preparation-x86_64-images","packet-impact"):
                    p="invocation-"+ph+"/"
                    if p+"verdict.json" not in names:continue
                    v=obj(p+"verdict.json")
                    stage=public["stages"][ph]={"status":v.get("status"),"exit":v.get("native_exit")}
                    stage["per_command_uuid_field_present"]="invocation_id" in v
                    if p+"stderr.log" in names:
                        stage["command_stderr"]=typed(data(p+"stderr.log").decode(errors="replace"))
                    if p+"events.jsonl" not in names:continue
                    ev=[json.loads(e) for e in data(p+"events.jsonl").splitlines()]
                    stage["child"]=[str(UUID(e["started"]["uuid"])) for e in ev if re.fullmatch(r"[0-9a-f-]{36}",e.get("started",{}).get("uuid",""))]
                    stage["progress"]=typed("\n".join(e.get("progress",{}).get(f,"") for e in ev for f in ("stderr","stdout")))
                p="invocation-packet-impact/"
                if p+"events.jsonl.manifest.json" in names:
                    ctx=obj(p+"context.json")
                    assert ctx["expected_source"]==SOURCE and data(p+"head.txt").decode().strip()==SOURCE
                    public["native_finished_at"]=datetime.fromisoformat(ctx["execution_finished_at"].replace("Z","+00:00")).isoformat()
                    man=obj(p+"events.jsonl.manifest.json");ev=data(p+"events.jsonl")
                    assert ident(ev)["sha256"]==man["events"]["sha256"] and len(ev)==man["events"]["bytes"]
                    es=[json.loads(e) for e in ev.splitlines()]
                    obs=obj("dsack-observations.json") if "dsack-observations.json" in names else {}
                    if obs: assert obs["source"]==SOURCE and obs["parent"]=="c451fa7d68e7b7809142c5daba0cd46a9cffe4fc"
                    for label in OWNERS:
                        row=public["owners"][label]={}
                        ers=[e for e in es if e.get("id",{}).get("testResult",{}).get("label")==label and "testResult" in e]
                        row["bep_count"]=len(ers)
                        if len(ers)!=1:continue
                        e=ers[0];row["status"]=e["testResult"]["status"]
                        rr=[r for r in man["reports"] if r["identity"]==e["id"]["testResult"]]
                        pub=obs.get("owners",{}).get(label,{})
                        row["original_verifier"]=pub.get("verified") is True
                        row["original_verifier_reason"]=typed(pub.get("error",""))
                        row["spawns"]=[]
                        for a in pub.get("observed_spawns",[]):
                            if not a.get("executable","").endswith("/test-setup.sh"):continue
                            props={v["name"]:v["value"] for v in a["platform"]["properties"]}
                            asset=any(x["path"]==r["path"].removeprefix("reports/execroot/_main/") and x.get("digest",{}).get("hash")==r["sha256"] and int(x["digest"]["sizeBytes"])==r["bytes"] for x in a.get("actualOutputs",[]) if "digest" in x for r in rr)
                            row["spawns"].append({"remote":a.get("runner")=="remote","cached":bool(a.get("cacheHit")),"exit":a.get("exitCode"),"linux_amd64_firecracker":all(props.get(k)==v for k,v in {"OSFamily":"linux","Arch":"amd64","workload-isolation-type":"firecracker","network-enable-ipv6":"true"}.items()),"physical_report_matches":asset})
                        caps=[]
                        for r in rr:
                            d=data(p+r["path"]);assert ident(d)=={k:r[k] for k in ("bytes","sha256")}
                            if r["name"]=="test.log":
                                s=d.decode(errors="replace");row["log"]=ident(d)
                                row["named_log"]=[k for k in ("--- FAIL: ","--- PASS: ","--- SKIP: ") if k+CASE in s]
                                row["stream_errors"]=sum(s.count(k) for k in ("recv after stream =","recv = "))
                                row["log_reason"]=typed(s)
                                ds=[]
                                for m in re.finditer(r"ACK at offset (\d+) has SACK blocks (\[[^\]]*\]), want (\[[^\]]*\]) \(base (\d+)\)",s):
                                    base=int(m[4]);ds.append({"ack":int(m[1]),"got":[[(int(x)-base)%2**32,(int(y)-base)%2**32] for x,y in re.findall(r"\{(\d+) (\d+)\}",m[2])],"want":[[(int(x)-base)%2**32,(int(y)-base)%2**32] for x,y in re.findall(r"\{(\d+) (\d+)\}",m[3])]})
                                row["block_errors"]=ds[:8]
                            if r["name"]=="test.xml":
                                ns=ET.fromstring(d).findall(".//testcase")
                                row["xml"]=ident(d)|{"case_count":len(ns),"named":sum(n.get("name")==CASE for n in ns),"failed":sum(n.find("failure") is not None or n.find("error") is not None for n in ns),"skipped":sum(n.find("skipped") is not None for n in ns)}
                            if r["name"].endswith(".pcap"):caps.append(d)
                            if r["name"]=="test.outputs__outputs.zip":
                                with zipfile.ZipFile(io.BytesIO(d)) as z:
                                    for i in z.infolist():
                                        if i.filename.endswith(".pcap"):
                                            assert i.file_size<=8<<20 and len(caps)<12;caps.append(z.read(i))
                        row["captures"]=[]
                        for d in caps:
                            cap=ident(d);row["captures"].append(cap)
                            try:
                                w=inspect_pcap(d)
                                cap["observations"]=[{k:f[k] for k in ("sent","ack","blocks")} for f in w["frames"]]
                                stored=pub.get("wire")
                                if stored:
                                    public_wire={k:v for k,v in stored.items() if k!="name"}
                                    cap["original_projection_equal"]=public_wire==w
                                    cap["canonical_projection_equal"]=public_wire==json.loads(json.dumps(w))
                            except (KeyError,ValueError,AssertionError,struct.error) as exc:
                                cap["error"]=trace(exc)
                                try:
                                    filtered=isolate_flow(d)
                                    w=inspect_pcap(filtered)
                                    cap["original_filtered_flow"]=ident(filtered)
                                    cap["observations"]=[{k:f[k] for k in ("sent","ack","blocks")} for f in w["frames"]]
                                except (KeyError,ValueError,AssertionError,struct.error) as inner:
                                    cap["filtered_error"]=trace(inner)
        if not public.get("owners") or any(not r.get("captures") for r in public["owners"].values()):
            page="0000";pages=[];seen=set()
            for _ in range(8):
                seen.add(page);j=get("GetLog",{"selector":{"invocation_id":ID},"page_token":page},1<<20)
                s=j.get("log",{}).get("contents","");pages.append(s)
                page=j.get("nextPageToken",j.get("next_page_token",""))
                if not page or page in seen:break
            public["original_log_clues_only"]=typed("\n".join(pages));public["original_log_clues_only"]["pages"]=len(pages)
    except Exception as exc:public["read_error"]=trace(exc)
    finally:
        key=None
        (root/"verdict.json").write_text(json.dumps(public,indent=2)+"\n")
    if "read_error" in public or "archive" not in public or set(public["owners"])!=set(OWNERS) or any(not row.get("bep_count") for row in public["owners"].values()):
        raise SystemExit(1)

if __name__=="__main__": main()
