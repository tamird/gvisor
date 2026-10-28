"""Publish only fields derived from authenticated original test reports."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tarfile
from uuid import UUID
import xml.etree.ElementTree as ET
def sha(b): return hashlib.sha256(b).hexdigest()
def report(task):
    r=json.loads((task/"receipt.json").read_bytes())
    native=r["native_invocation"]
    out={"source":SOURCE,"request":REQUEST,"invocation":str(UUID(native["invocationId"])),"parent":r["status"],"producer":r.get("producer_status"),"collection":r["collection_status"],"phases":{}}
    if r.get("consumer_verified_at") and native.get("durationUsec"):
        finish=datetime.fromtimestamp((int(native["createdAtUsec"])+int(native["durationUsec"]))/1e6,timezone.utc)
        verified=datetime.fromisoformat(r["consumer_verified_at"].replace("Z","+00:00"))
        out.update(finished=finish.isoformat(),verified=verified.isoformat(),delay_s=(verified-finish).total_seconds())
    f=task/"qualification.tar.gz"
    if not f.is_file():return out
    raw=f.read_bytes();out["archive"]={"sha256":sha(raw),"bytes":len(raw)}
    assert out["archive"]=={k:r["artifact"][k] for k in out["archive"]}
    with tarfile.open(f) as t:
        def read(name):
            ent=t.getmember(name);assert ent.isreg() and 0<=ent.size<=96<<20
            return t.extractfile(ent).read()
        def doc(name):return json.loads(read(name))
        members=set(t.getnames());summary=doc("epoll-checklocks.json") if "epoll-checklocks.json" in members else {}
        for phase,(source,labels) in PHASES.items():
            pre="invocation-"+phase+"/";s=summary.get(phase,{});part=out["phases"][phase]={"source":source,"verified":s.get("verified") is True,"owners":{}}
            if s.get("error"):part["callback_lines"]=[int(i) for i in re.findall(r'image_workload[.]py", line ([0-9]+)',s["error"])][:4]
            if pre+"events.jsonl" not in members:continue
            assert read(pre+"head.txt").decode().strip()==source
            b=read(pre+"events.jsonl");man=doc(pre+"events.jsonl.manifest.json")
            assert (sha(b),len(b))==(man["events"]["sha256"],man["events"]["bytes"])
            es=[json.loads(x) for x in b.splitlines()];[uid]=[v["started"]["uuid"] for v in es if "started" in v]
            part["child"]=str(UUID(uid));part["native_exit"]=int(doc(pre+"verdict.json")["native_exit"])
            for label in labels:
                matches=[e for e in es if e.get("id",{}).get("testResult",{}).get("label")==label and "testResult" in e]
                if not matches:continue
                [e]=matches;original=e["testResult"];v=part["owners"][label]={"status":original["status"]}
                refs={m["name"]:m for m in man["reports"] if m["identity"]==e["id"]["testResult"] and m["name"] in ("test.xml","test.log")}
                if set(refs)!={"test.log","test.xml"}:continue
                bodies={k:read(pre+m["path"]) for k,m in refs.items()}
                assert all((sha(bodies[k]),len(bodies[k]))==(m["sha256"],m["bytes"]) for k,m in refs.items())
                v["reports"]={k:{"sha256":sha(b),"bytes":len(b)} for k,b in bodies.items()}
                xml=ET.fromstring(bodies["test.xml"]);nodes=xml.findall(".//testcase")
                names=[n.get("name") for n in nodes];fail=[n.get("name") for n in nodes if n.find("failure") is not None or n.find("error") is not None];skip=[n.get("name") for n in nodes if n.find("skipped") is not None]
                v.update(cases=len(nodes),failed=len(fail),skipped=len(skip),selected={k:("failed" if k in fail else "skipped" if k in skip else "passed") for k in SELECTION if k in names})
                log=bodies["test.log"].decode(errors="replace"); v["marker"]={a:next((tag for tag in ("PASS","FAIL") if re.search(a+r"\s*\.\.\.\s*"+tag,log,re.I)),None) for a in ("amd64","arm64")} if label in ANALYZERS else {}
                counts=re.findall(r"epoll MOD change-mask=(false|true) allocations/call ([0-9]+\.[0-9]+)",log);v["alloc"]={k:float(i) for k,i in counts if float(i)<1024}
                v["restore_disarm_mismatch"]="after restore: recheck queued = true, want false" in log and any(n.get("name")==R+"/disarmed" and "after restore: recheck queued = true, want false" in ET.tostring(n,encoding="unicode") for n in nodes)
                v["diagnostics"]=[]
                for line in log.splitlines():
                    if not ("checklocks" in line or "must be locked" in line or "must hold" in line):continue
                    loc=re.search(r"(epoll|file_description|save_restore)[.]go:(\d+)(?::\d+)?:",line)
                    kind=next((k for k,sig in (("writer","when accessing replacement"),("accessor","to call activeEntry"),("sequence","when accessing readySeq"),("index","when accessing interest"),("queue","queueReadyAndUnlock")) if sig in line),"other")
                    if loc:v["diagnostics"].append([kind,loc[1],int(loc[2])])
                physical=s.get("owners",{}).get(label,{})
                if physical and physical["xml"]==sha(bodies["test.xml"]):
                    if re.fullmatch("[0-9a-f]{64}",physical["action"]):v["action"]=physical["action"]
                    v["xml_actions"]=[h for h in physical["xml_actions"] if re.fullmatch("[0-9a-f]{64}",h)][:8]
    return out
BASE = "b7cbb86f1edf369a28bd27d1c4817eab917abb8c"
OLD = "bbf65d6f80d666b7c44f34d7d8c0fa863fe7b222"
PRODUCT = "710c59cdadfb5f222b6a323a3b5eabeec7940a5a"
FIX = "7e539708e684d583eef3bef82862cebce5285654"
BEFORE = "3d34483f9f4dda76fdb977d5978ddefb0032b417"
TREES = {BASE:"ef51e8dcd55330bac84914fd82ef959d785ab79a",OLD:"2e0c5e6e10a7452a88d415416dd8d05c21cc89a5",PRODUCT:"810749dc1ee9122f6fa0f425eb0835d289de139d",FIX:"4e0b06638a8062d4b418cfcf09366bf91117cb4e",BEFORE:"2bdf3f31281df110fdefaa2612c080621cc89128"}
VFS = "//pkg/sentry/vfs:vfs_test"
CONTRACT = "//pkg/sentry/epollqualification:epoll_contract_test"
NOGO = "//pkg/sentry/vfs:vfs_nogo"
ANALYZERS = (NOGO, VFS + "_nogo")
OWNERS = [*ANALYZERS,VFS,CONTRACT]
R = "TestEpollRestoreOneShot"
RESTORE_NAMES = {R,R+"/armed",R+"/disarmed"}
PHASES = {"guards": (FIX,ANALYZERS), "race":(FIX,(VFS,)), "restore":(FIX,(CONTRACT,)), "before":(BEFORE,(VFS,))}

SOURCE=FIX
REQUEST='9bdff003027d7e721b831ecc24a7f40d886769ae7ffd305295444aab6f1b35f8'
SELECTION=['TestEpollFailedModify','TestEpollFailedRegistration','TestModifiedEpollCheckpoint','TestEpollModifyAllocations','TestLinuxEpollModify']+['TestEpollFailedModify/'+v for v in ('pending_edge','delivered_edge','disarmed_one_shot')]

SELECTION.extend(sorted(RESTORE_NAMES))

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
    except BaseException as exc:
        rc=1
        public["projection_error"]=type(exc).__name__ if type(exc).__name__ in ("KeyError","ValueError","AssertionError","TypeError","IndexError","FileNotFoundError","StopIteration") else "OTHER"
        tb=exc.__traceback__;lines=[]
        while tb:
            if Path(tb.tb_frame.f_code.co_filename).name=="receipt.py":lines.append(tb.tb_lineno)
            tb=tb.tb_next
        public["projection_lines"]=lines[:8]
    finally:
        key=None
        marker=task/"submission.json"
        if marker.is_file():public["invocation"]=str(UUID(json.loads(marker.read_bytes())["invocationId"]))
        (root/"verdict.json").write_text(json.dumps(public,indent=2)+"\n")
    raise SystemExit(rc)
