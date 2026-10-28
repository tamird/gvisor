"""Publish typed observations from the original authenticated epoll receipt."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tarfile
from uuid import UUID
import xml.etree.ElementTree as ET

SOURCE="e070894f5170984643df90e9f0bbe100c87329a7"
STOCK="81c2e35f06fc1d0ba9b7dee48a4fc0af3b691978"
REQUEST="fbd653480396cc04fbac1b6ea52222516818364b2d3166cdc85d75f69bf4dfb0"
VFS="//pkg/sentry/vfs:vfs_test"
FUSE="//pkg/sentry/fsimpl/fuse:fuse_test"
CONTRACT="//test/rbe:epoll_contract_test"
ANALYZERS=("//pkg/sentry/vfs:vfs_nogo",VFS+"_nogo")
PHASES={"stock":(STOCK,(VFS,FUSE,CONTRACT)),"fixed":(SOURCE,(VFS,CONTRACT,*ANALYZERS)),"fixed-fuse":(SOURCE,(FUSE,)),"fixed-race":(SOURCE,(VFS,))}
MOD="TestEpollFailedModify"
NAMES={MOD,*(MOD+"/"+n for n in ("pending_edge","delivered_edge","disarmed_one_shot")),"TestEpollFailedRegistration","TestModifiedEpollCheckpoint","TestEpollModifyAllocations","TestLinuxEpollModify","TestEpollModifyAfterControlledAbort"}

def number(n,limit=2**40):
    if type(n) is not int or not 0<=n<=limit: raise ValueError("invalid count")
    return n

def digest(s):
    if not isinstance(s,str) or not re.fullmatch("[0-9a-f]{64}",s): raise ValueError("invalid digest")
    return s

def enum(s):
    return s if s in ("PASS","PASSED","FAIL","FAILED","INCOMPLETE","COMPLETE","TIMEOUT","ERROR") else "UNKNOWN"

def iso(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc).isoformat()

def report(task):
    r=json.loads((task/"receipt.json").read_bytes())
    native=r["native_invocation"]
    out={"source":SOURCE,"stock":STOCK,"request":REQUEST,"invocation":str(UUID(native["invocationId"])),"parent_status":enum(r["status"]),"producer_status":enum(r.get("producer_status")),"collection":enum(r["collection_status"]),"phases":{}}
    if native.get("createdAtUsec") and native.get("durationUsec") and r.get("consumer_verified_at"):
        finish=datetime.fromtimestamp((int(native["createdAtUsec"])+int(native["durationUsec"]))/1e6,timezone.utc)
        verified=iso(r["consumer_verified_at"])
        out.update(finished=finish.isoformat(),verified=verified,finish_to_verified_ms=round(1000*(datetime.fromisoformat(verified)-finish).total_seconds(),3))
    if r["collection_status"]!="COMPLETE" or not (task/"qualification.tar.gz").is_file():return out
    raw=(task/"qualification.tar.gz").read_bytes()
    assert sha256(raw).hexdigest()==r["artifact"]["sha256"]
    out["archive"]={"sha256":digest(r["artifact"]["sha256"]),"bytes":number(len(raw))}
    with tarfile.open(task/"qualification.tar.gz") as archive:
        members=set(archive.getnames())
        def get(name,maxlen=16<<20):
            m=archive.getmember(name)
            assert m.isreg() and 0<=m.size<=maxlen
            return archive.extractfile(m).read()
        def read(name):return json.loads(get(name))
        rows=read("epoll-modify.json") if "epoll-modify.json" in members else {}
        assert not rows or (rows["source"],rows["stock"])==(SOURCE,STOCK)
        all_verified=True
        for phase,(source,labels) in PHASES.items():
            prefix="invocation-"+phase+"/"
            item=out["phases"][phase]={"source":source,"owners":{},"verified":False}
            if prefix+"events.jsonl" not in members:
                all_verified=False;continue
            assert get(prefix+"head.txt").decode().strip()==source
            bep=get(prefix+"events.jsonl",96<<20)
            manifest=read(prefix+"events.jsonl.manifest.json")
            assert (len(bep),sha256(bep).hexdigest())==(manifest["events"]["bytes"],manifest["events"]["sha256"])
            events=[json.loads(line) for line in bep.splitlines()]
            [child]=[e["started"]["uuid"] for e in events if "started" in e]
            item["child"]=str(UUID(child))
            verdict=read(prefix+"verdict.json")
            item.update(status=enum(verdict["status"]),native_exit=number(verdict["native_exit"],255))
            native_record=next(v for v in r["invocations"] if v["path"]==prefix+"verdict.json")
            if native_record.get("execution_finished_at"):item["finished"]=iso(native_record["execution_finished_at"])
            stored=rows.get("phases",{}).get(phase,{})
            item["verified"]=stored.get("verified") is True
            all_verified &= item["verified"]
            for label in labels:
                original=[e for e in events if e.get("id",{}).get("testResult",{}).get("label")==label and "testResult" in e]
                if len(original)!=1:
                    all_verified=False;continue
                event=original[0]
                owner=item["owners"][label]={"status":enum(event["testResult"]["status"]),"reports":{}}
                primary={}
                for ref in manifest["reports"]:
                    name=ref["name"]
                    if ref["identity"]!=event["id"]["testResult"] or name not in ("test.log","test.xml"):continue
                    raw=get(prefix+ref["path"])
                    assert name not in primary and (len(raw),sha256(raw).hexdigest())==(ref["bytes"],ref["sha256"])
                    primary[name]=raw
                    owner["reports"][name]={"bytes":number(len(raw)),"sha256":digest(ref["sha256"])}
                assert set(primary)=={"test.xml","test.log"}
                nodes=ET.fromstring(primary["test.xml"]).findall(".//testcase")
                def status(n):return "FAILED" if n.find("failure") is not None or n.find("error") is not None else "SKIPPED" if n.find("skipped") is not None else "PASSED"
                owner["counts"]={s:sum(status(n)==s for n in nodes) for s in ("PASSED","SKIPPED","FAILED")}
                owner["selected"]={n.get("name"):status(n) for n in nodes if n.get("name") in NAMES}
                owner["stock_event_mismatch"]=[n.get("name") for n in nodes if n.get("name") in NAMES and "epoll_wait: got " in "".join(n.itertext())]
                if label==FUSE:owner["aborted_fuse_mod_eperm"]=b"same-mask MOD on aborted DeviceFD: operation not permitted" in primary["test.log"]
                physical=stored.get("owners",{}).get(label)
                if physical:
                    owner["test_action"]=digest(physical["action"]["digest"]["hash"])
                    owner["primary_xml_actions"]=[digest(x) for x in physical["xml_actions"]][:8]
                if label==CONTRACT:
                    counts=re.findall(rb"epoll MOD change-mask=(false|true) allocations/call ([0-9]+\.[0-9]+)",primary["test.log"])
                    owner["allocations_per_call"]={k.decode():float(v) for k,v in counts if float(v)<1024}
                if label in ANALYZERS:
                    owner["analysis_target_cpus"]=[a for a in ("amd64","arm64") if re.search(a.encode()+rb"\s*\.\.\.\s*PASS",primary["test.log"],flags=re.I)]
        out["result"]="CONFIRMED_DIFFERENTIAL" if all_verified else "INCOMPLETE"
    return out

class Quiet:
    def write(self,s):
        if re.fullmatch(r"https://app\.buildbuddy\.io/invocation/[0-9a-f-]{36}",s):
            print(s,file=sys.__stdout__,flush=True)
        return len(s)
    def flush(self):pass

def collect(key):
    root=Path(os.environ["RUNNER_TEMP"])/"epoll-modify"
    task=root/"task"
    public={"source":SOURCE,"request":REQUEST,"stage":"submit"}
    rc=1
    try:
        assert key
        sender=runpy.run_path(str(task/"submit.py"))
        with redirect_stdout(Quiet()),redirect_stderr(Quiet()):
            rc=sender["main"](lambda _:key)
        key=None
        public["stage"]="reconcile"
        public=report(task)
    except BaseException:
        rc=1
    finally:
        key=None
        marker=task/"submission.json"
        if marker.is_file():public["invocation"]=str(UUID(json.loads(marker.read_bytes())["invocationId"]))
        (root/"verdict.json").write_text(json.dumps(public,indent=2)+"\n")
    raise SystemExit(rc)
