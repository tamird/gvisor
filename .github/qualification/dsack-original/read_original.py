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


# Only words from this fixed diagnostic vocabulary can leave the private runner.
ERROR_WORDS=set("""failed fail failure fatal error errors executing command action build bazel remote executor executors execution worker workers available unavailable no matching eligible matches matched platform properties property isolation-type workload-isolation-type network-enable-ipv6 architecture arch amd64 linux firecracker container-image constraint constraints requested not found package target packages targets incompatible option invalid illegal argument arguments flag configuration config configured compiler compile compiling linking linker link running exited exit code terminated killed signal resource exhausted deadline exceeded timeout timed out memory disk space filesystem file directory header required missing declared support supported unsupported unsupported class strict dependency dependencies imports cannot use find undefined load loading image pulling docker toolchain c++ gcc g++ cc clang ld ar go cppcompile gocompilepkg golink genrule test test-setup unexpected internal external exception java io server busy scheduling denied unauthenticated permission forbidden authorization denied rate capacity available register registered architecture pool allowed sandbox connection server artifact from for on to with by of and or in while the an a as this that is was are were only too many too large too small true false tcpdump tshark pcap libpcap disallowed CGO gccgo filegroup read download compiler memory expected different requiring permissions quota actionkey filesystem stale error checking repository packages retry attempts interrupted core dumped runtime cache network protobuf protoc mapper syntax dependency generation conflict flag resourceexhausted deadlineexceeded failedprecondition outofrange notfound unauthenticated permissiondenied cancelled invalidargument aborted internal unknown no matching architectures disabled enabled rejected no-sandbox mode base priv capabilities --config network-enable-ipv6 exec properties input output state checker fork exec errno eacces enoent sigkill sigterm killed task test gcc assembler collect2 clang++ rule repos visibility incompatible ignored project source invalid arguments dependencies failed to initialize setup unable find registered execution remote execution errors cpu cores action result node platform properties build grpc rpc capped operations invoking option incompatible changes cpu request memory overhead not enough signing region namespace operation implements interfaces aborted timeout systrap ptrace docker process status version working file object executable cpu milliseconds no such host stdout stderr data discarded flakes node excluded allowed rbe buildbuddy linux-sandbox nonzero linux-gnu tests ran success successfully unsuccessful did not complete no run could not start""".lower().split())
DIAG_KEYS=("error","failed","cannot","fatal","aborted","exception","missing","denied","timeout","killed","unavailable","exhausted","not found","no such","not supported","not match","invalid","incompatible","no registered","not allowed","did not","wrong","unable","not enough","disallowed","expired","reason","rejected","no tests")
def include_identity(text):
    # Only compiler include roots and public Abseil source paths have meaning
    # for this confirmed UNDECLARED_INCLUSIONS failure.
    roots=sorted(set(re.findall(r"(/usr/(?:lib/gcc/[a-z0-9_-]+/[0-9.]+/include(?:-fixed)?|include/c[+][+]/[0-9.]+|include/[a-z0-9_-]+/c[+][+]/[0-9.]+)(?:/[a-zA-Z0-9_.+/-]+\.(?:h|hpp|inc)|/[a-zA-Z0-9_+-]+))(?=[ '\"\n\r,;:]|$)",text)))
    src=sorted(set(re.findall(r"(?:^|[/\s'\"])(absl/[a-zA-Z0-9_.+/-]+[.](?:cc|h|inc))(?=[ '\"\n\r,;:]|$)",text)))
    return {"compiler_include_paths":[x for x in roots if ".." not in Path(x).parts][:40],
            "absl_source_files":[x for x in src if ".." not in Path(x).parts][:30]}


def details(s):
    lines=[]
    for line in re.sub(r"\x1b\[[;\d]*[A-Za-z]","",s).replace("\r","\n").splitlines():
        if not any(x in line.lower() for x in DIAG_KEYS):continue
        if len(line)>1500 or re.search(r"(?i)(api.?key|credential|authorization[:=]|secret|bearer|password|remote_header|BEGIN .*PRIVATE|https?://[^/ ]+@)",line):continue
        words=re.findall(r"[a-zA-Z][a-zA-Z0-9_+.-]*",line)
        normalized=" ".join(w for w in (k.lower().strip(".-") for k in words) if w in ERROR_WORDS)
        if normalized and normalized not in lines:lines.append(normalized[:420])
    root=Path(os.environ["GITHUB_WORKSPACE"])
    sources=[]
    for f,n in re.findall(r"(?<![\w.-])((?:pkg|test|runsc|tools)/[a-zA-Z0-9_./-]{2,175}[.](?:go|cc|cpp|c|h|py|bzl)):(\d{1,6})",s):
        if ".." not in Path(f).parts and (root/f).is_file() and (f,int(n)) not in sources:sources.append((f,int(n)))
    return {"terms":(lines[:8]+lines[-4:] if len(lines)>12 else lines),"source_lines":sources[:12],
            "exit_codes":sorted({int(x) for x in re.findall(r"(?i)(?:exit(?:ed)?(?: with)? (?:code |status )?|signal )([0-9]{1,3})(?:\W|$)",s)})[:12]}


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
        assert method in ("GetInvocation","GetLog","GetFile")
        c=HTTPSConnection("app.buildbuddy.io", timeout=25)
        try:
            c.request("POST","/api/v1/"+method,json.dumps(body),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
            with c.getresponse() as r:
                assert r.status==200
                b=r.read(limit+1);assert len(b)<=limit
                return b if method=="GetFile" else json.loads(b)
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
                        cmdtext=data(p+"stderr.log").decode(errors="replace")
                        stage["command_stderr"]=typed(cmdtext)
                        if ph=="packet-impact":stage["stderr_diagnostic"]=details(cmdtext);stage["includes"]=include_identity(cmdtext)
                    if p+"events.jsonl" not in names:continue
                    ev=[json.loads(e) for e in data(p+"events.jsonl").splitlines()]
                    stage["child"]=[str(UUID(e["started"]["uuid"])) for e in ev if re.fullmatch(r"[0-9a-f-]{36}",e.get("started",{}).get("uuid",""))]
                    progress="\n".join(e.get("progress",{}).get(f,"") for e in ev for f in ("stderr","stdout"))
                    stage["progress"]=typed(progress)
                    if ph!="packet-impact":continue
                    stage["progress_diagnostic"]=details(progress)
                    stage["failures"]=[]
                    stderr_reads=0
                    for event in ev:
                        for k in ("action","aborted","finished","completed"):
                            if k not in event:continue
                            val=event[k]
                            if k=="action" and val.get("success") is True:continue
                            if k=="completed" and val.get("success") is True:continue
                            row={"kind":k};d=val.get("failureDetail",{})
                            row["codes"]={n:v.get("code") for n,v in d.items() if isinstance(v,dict) and re.fullmatch(r"[A-Z0-9_]{1,90}",v.get("code",""))}
                            if k=="aborted" and re.fullmatch(r"[A-Z_]{1,60}",val.get("reason","")):row["aborted"]=val["reason"]
                            if k=="finished" and isinstance(val.get("exitCode"),dict):row["exit"]={"code":val["exitCode"].get("code"),"name":val["exitCode"].get("name") if re.fullmatch(r"[A-Z_]{1,80}",val["exitCode"].get("name","")) else None}
                            if k=="action" and re.fullmatch(r"[A-Za-z0-9]{1,60}",val.get("type","")):row["mnemonic"]=val["type"]
                            msg=d.get("message","")+"\n"+val.get("description","")
                            row["message"]=typed(msg);row["message_diagnostic"]=details(msg);row["includes"]=include_identity(msg)
                            eid=next((x for x in event.get("id",{}).values() if isinstance(x,dict) and x.get("label")),{}).get("label","")
                            if re.fullmatch(r"(?:@@?)?//(?:pkg|test|tools|runsc)/[a-zA-Z0-9_./-]+:[a-zA-Z0-9_+.-]+",eid):row["public_owner"]=eid
                            if k=="action" and stderr_reads<2:
                                uri=val.get("stderr",{}).get("uri","")
                                u=urlsplit(uri);m=re.search(r"/blobs/([0-9a-f]{64})/(\d+)$",u.path)
                                if u.scheme=="bytestream" and u.hostname=="remote.buildbuddy.io" and m and int(m[2])<=524288:
                                    stderr_reads+=1
                                    try:
                                        b=get("GetFile",{"uri":uri},int(m[2]))
                                        assert len(b)==int(m[2]) and sha256(b).hexdigest()==m[1]
                                        st=b.decode(errors="replace");row["action_stderr"]=ident(b)|{"typed":typed(st),"diagnostic":details(st)}
                                    except Exception as ex:row["stderr_read_error"]=trace(ex)
                            if len(stage["failures"])<18:stage["failures"].append(row)
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
        if public.get("owners") and any(r.get("bep_count") for r in public["owners"].values()) and any(not r.get("captures") for r in public["owners"].values()):
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
