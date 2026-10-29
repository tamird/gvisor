"""Publish typed observations from the original authenticated Eifel receipt."""
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

SOURCE="0758e73f2109efdf99a9bd93cf97a8735622206b"
STOCK="499d0dcec661f36b7a7eb9dce92dd23135be91ca"
REQUEST="a27a579af1dc1177c38f2a071a8cb25b32d34773b7c6a84a8dd010d05324d7f4"
OWNER="//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
NOGO=("//pkg/tcpip/transport/tcp:tcp_nogo",OWNER+"_nogo")
PHASES={"prior-head-controls":(STOCK,(OWNER,)),"corrected-sack":(SOURCE,(OWNER,)),"corrected-nogo":(SOURCE,NOGO)}
CASE="TestDetectSpuriousRecoveryWithRTO"
NAMES={CASE,*(CASE+"/"+x for x in ("timestamps","no_timestamps","missing_timestamp","zero_echo"))}
DIAGNOSTICS=("SpuriousRecovery counter = 1, want 0","SpuriousRTORecovery counter = 1, want 0")

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
    if r["collection_status"]!="COMPLETE":
        out["collection_error_kind"]="source-set" if "Producer sources " in r.get("collection_error","") else "collection"
        if native.get("artifact"):out["advertised_archive"]={"bytes":number(native["artifact"]["bytes"]),"sha256":digest(native["artifact"]["sha256"])}
        return out
    if not (task/"qualification.tar.gz").is_file():return out
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
        rows=read("eifel-review.json") if "eifel-review.json" in members else {}
        assert not rows or (rows["fix"],rows["control"])==(SOURCE,STOCK)
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
            if stored.get("diagnostic"):
                item["callback_lines"]=[int(x) for x in re.findall(r'image_workload[.]py", line ([0-9]+)',stored["diagnostic"])][:8]
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
                owner["zero_echo_counter_failures"]=[key.split(" ",1)[0] for key in DIAGNOSTICS if key.encode() in primary["test.log"] and any(n.get("name")==CASE+"/zero_echo" and key in "".join(n.itertext()) for n in nodes)]
                owner["target_cpus"]=[cpu for cpu in ("amd64","arm64") if re.search(cpu.encode()+rb"\s*\.\.\.\s*PASS",primary["test.log"],re.I)] if label!=OWNER else []
                physical=stored.get("owners",{}).get(label,{})
                owner["workers"]=[{"sha256":digest(v["digest"]["hash"]),"reports":[n for n in ("test.log","test.xml") if n in v["reports"]],"exit":number(v["exit"],255)} for v in physical.get("workers",[])][:3]
                if physical.get("diagnostic"):owner["callback_lines"]=[int(x) for x in re.findall(r'image_workload[.]py", line ([0-9]+)',physical["diagnostic"])][:8]
        original=out["phases"]["prior-head-controls"]["owners"].get(OWNER,{})
        fixed=out["phases"]["corrected-sack"]["owners"].get(OWNER,{})
        negative=(original.get("selected")=={n:("FAILED" if n in (CASE,CASE+"/zero_echo") else "PASSED") for n in NAMES}
                  and original.get("counts")=={"FAILED":2,"PASSED":3,"SKIPPED":0}
                  and len(original.get("zero_echo_counter_failures",[]))==2)
        corrected=(fixed.get("selected")=={n:"PASSED" for n in NAMES} and fixed.get("counts",{}).get("FAILED")==0 and fixed.get("counts",{}).get("SKIPPED")==0)
        ana=all(out["phases"]["corrected-nogo"]["owners"].get(label,{}).get("target_cpus")==["amd64","arm64"] for label in NOGO)
        out.update(control_observed=negative,corrected_observed=corrected,analyzers_observed=ana,
                   result="CONFIRMED_DIFFERENTIAL" if negative and corrected and ana and all_verified else "INCOMPLETE")
    return out

class Quiet:
    def write(self,s):
        if re.fullmatch(r"https://app\.buildbuddy\.io/invocation/[0-9a-f-]{36}",s):
            print(s,file=sys.__stdout__,flush=True)
        return len(s)
    def flush(self):pass

def collect(key):
    root=Path(os.environ["RUNNER_TEMP"])/"eifel-review"
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
        if public.get("result")!="CONFIRMED_DIFFERENTIAL":rc=1
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
