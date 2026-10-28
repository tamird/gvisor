"""Publish only the predeclared supplier comparison facts."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tarfile
from uuid import UUID

SOURCE = "9107384786056cdc05c9ad869bc07a577c66e746"
CANDIDATE = "01446201fd8e758267f4e1abd9727926655110b7"
REQUEST = "15a52682dbd12af6ff738472ac096f6434b02bf851b6cb072dbe0f98def2df99"
OWNERS = {"//tests/deb:pkg_deb_test":12,"//tests/deb:control_field_test":6,"//tests/tar:pkg_tar_test":22,"//tests/tar:is_compressed_test":1}
PRODUCTS = {"//tests/deb:tar_input":["tar.gz"],"//tests/deb:test_deb":["deb","changes"],"//tests/tar:test-tar-preserve_mode-True":["tar"]}

def digest(s):
    assert type(s) is str and re.fullmatch("[0-9a-f]{64}",s)
    return s

def number(n,limit=2**32-1):
    assert type(n) is int and 0<=n<=limit
    return n

def enum(s):
    return s if s in {"PASS","FAIL","PASSED","FAILED","COMPLETE","INCOMPLETE","TIMEOUT","MATCHED_NATIVE_OBSERVATION"} else "UNKNOWN"

def pair(v):
    return {"sha256": digest(v[0]), "bytes": number(v[1])}

def action(x):
    return digest(x["action"]["hash"])

def duration(s):
    if type(s) is str and re.fullmatch(r"[0-9]+(\.[0-9]{1,9})?s",s):
        return s

def verdict(task):
    r=json.loads((task/"receipt.json").read_bytes())
    submitted=json.loads((task/"submitted.json").read_bytes())
    assert submitted["request"]==REQUEST and submitted["source"]==SOURCE and r["native_invocation"]["invocationId"]==submitted["invocation_id"]
    assert r.get("expected_sources")==[SOURCE] and r.get("sources") in ([],[SOURCE])
    result={"source":SOURCE,"stock":CANDIDATE,"observed_source":r.get("sources")==[SOURCE],"request":REQUEST,"invocation":str(UUID(submitted["invocation_id"])),"status":enum(r["status"]),"producer":enum(r.get("producer_status")),"capture":enum(r["collection_status"]),"phases":{}}
    f=task/"qualification.tar.gz"
    if not f.is_file() or not r.get("artifact"):return result
    archived=f.read_bytes();d=hashlib.sha256(archived).hexdigest();assert d==r["artifact"]["sha256"] and len(archived)==r["artifact"]["bytes"]
    result["archive"]={"sha256":digest(d),"bytes":number(len(archived))}
    with tarfile.open(f) as t:
        names=set(t.getnames())
        def item(n):
            return json.load(t.extractfile(n))
        for phase in ("dependency-preparation-x86_64-images","supplier-tests","zip-static-preparation","static-archives","zip-inspector"):
            pr="invocation-"+phase+"/";vname=pr+"verdict.json"
            if vname not in names:continue
            v=item(vname);row=result["phases"][phase]={"status":enum(v["status"]),"exit":number(v["native_exit"],255) if type(v.get("native_exit")) is int and 0<=v["native_exit"]<=255 else None}
            if pr+"events.jsonl" in names:
                for raw in t.extractfile(pr+"events.jsonl"):
                    event=json.loads(raw)
                    if "started" in event.get("id",{}) and event.get("started",{}).get("uuid"):row["child"]=str(UUID(event["started"]["uuid"]));break
        if "supplier-zip.json" not in names:return result
        z=item("supplier-zip.json");assert z["source"]==SOURCE and z["parent"]==CANDIDATE
        ref=z["original_default"]
        assert ref["parent"]=="a288512e-7fcd-4126-adce-232ddb22bfa9" and ref["child"]=="211053ef-e8f1-4653-acb7-710a5065ef37"
        result["original_default"]={"parent":str(UUID(ref["parent"])),"child":str(UUID(ref["child"])),"archive":pair(ref["archive"])}
        result["comparison"]=z["status"] if z["status"] in ("MATCHED_STATIC_REPLAY","INCOMPLETE") else "UNKNOWN"
        if z.get("error"):
            e=z["error"];result["error_sha256"]=digest(hashlib.sha256(e.encode()).hexdigest())
            result["error_kinds"]=[s for s in ("ValueError","KeyError","AssertionError","FileNotFoundError","HTTPError","TimeoutError","RuntimeError","incorporated","unexpected release leaf","incorrect static Python","static runtime","static toolchain analysis failed","static native package build failed","stock","writer ZIP","writer source","missing original replay","wrong interpreter","descriptor","ELF","physical","not enough values","too many values","mode","not authenticated") if s.lower() in e.lower()]
        result["owners"]={}
        for ph,obj in z["phases"].items():
            assert ph in ("supplier-tests","static-archives")
            rows=result["phases"][ph]["writers"]={}
            for l,raw in obj["writers"].items():
                assert l in PRODUCTS
                row=rows[l]={"action":action(raw),"zip_action":action(raw["zip_producer"]),"zip":pair(raw["zip"]),"interpreter":pair(raw["interpreter"]),"dynamic":bool(raw["elf_interpreter"]),"input_count":number(raw["input_count"]),"outputs":{}}
                met=raw["zip_metrics"];row["zip_metrics"]={k:number(met[k]) for k in ("entries","expanded_bytes","compressed_member_bytes")};row["zip_metrics"]["bootstrap"]=digest(met["bootstrap"])
                for name,dg in raw["outputs"].items():
                    [suffix]=[k for k in PRODUCTS[l] if name.endswith("."+k)]
                    assert type(raw["matches_stock"][suffix]) is bool
                    row["outputs"][suffix]={**pair(dg),"stock":raw["matches_stock"][suffix]}
                assert set(row["outputs"])==set(PRODUCTS[l])
                if "hello_mode" in raw:row["hello_mode"]=number(raw["hello_mode"],0o7777)
                for owner in ("writer_metrics","zip_producer_metrics"):
                    row[owner]={k:duration(v) for k,v in raw.get(owner,{}).items() if k in ("totalTime","executionWallTime","inputFetchTime","fetchTime","uploadTime") and duration(v) is not None}
            for l,raw in obj["owners"].items():
                assert l in OWNERS and raw["cases"]==OWNERS[l]
                row=result["owners"][l]={"status":enum(raw["status"]),"cases":number(raw["cases"]),"failed":number(raw["failed_cases"]),"action":action(raw),"primary":{}}
                for rr in raw["reports"]:
                    if rr["name"] in ("test.log","test.xml"):row["primary"][rr["name"]]={"sha256":digest(rr["sha256"]),"bytes":number(rr["bytes"])}
                assert set(row["primary"])=={"test.log","test.xml"}
        result["replays"]={}
        for l,raw in z["replays"].items():
            assert l in ("//tests/deb:tar_input","//tests/deb:test_deb")
            n="zip-replay-"+l.split(":")[-1]+".json";data=t.extractfile(n).read();assert len(data)==raw["record"]["bytes"] and hashlib.sha256(data).hexdigest()==raw["record"]["sha256"]
            detail=json.loads(data);out=result["replays"][l]={"record":pair([raw["record"]["sha256"],len(data)]),"status":enum(raw["status"]),"inspector_exit":number(raw["exit"],255)}
            for field in ("input_leaves","input_files","input_bytes"):
                if field in detail:out[field]=number(detail[field])
            for a in detail.get("attempts",[]):
                ar={"exit":number(a["exit"],255) if type(a.get("exit")) is int and 0<=a["exit"]<=255 else None,"matches":a.get("matches_original") is True}
                if a.get("started") and a.get("finished"):
                    delta=(datetime.fromisoformat(a["finished"].replace("Z","+00:00"))-datetime.fromisoformat(a["started"].replace("Z","+00:00"))).total_seconds()
                    if 0<=delta<600:ar["duration_ms"]=round(delta*1000,3)
                if type(a.get("errno")) is int:ar["errno"]=number(a["errno"],4096)
                ar["outputs"]={next(k for k in PRODUCTS[l] if path.endswith("."+k)):pair(v) for path,v in a["outputs"].items()}
                out["attempt"]=ar
        if result["comparison"]=="MATCHED_STATIC_REPLAY":assert not result["owners"] and len(result["replays"])==2 and all(z["status"]=="PASS" for z in result["replays"].values())
    return result

class Quiet:
    def write(self,s):
        s=s.strip()
        if re.fullmatch(r"https://app\.buildbuddy\.io/invocation/[0-9a-f-]{36}",s):
            print(s,file=sys.__stdout__,flush=True)
        return len(s)
    def flush(self):
        return None

def collect(key):
    root=Path(os.environ["RUNNER_TEMP"])/"supplier-zip-static"
    task=root/"task"/"job"
    sys.path.insert(0,str(task))
    public={"source":SOURCE,"stock":CANDIDATE,"request":REQUEST,"stage":"submit"}
    rc=1
    try:
        with redirect_stdout(Quiet()),redirect_stderr(Quiet()):
            submit=runpy.run_path(str(task/"submit.py"))
            rc=submit["main"](lambda _:key)
        key=None
        public["stage"]="reconcile"
        public=verdict(task)
    except BaseException as exc:
        rc=1
        typ=type(exc).__name__
        public["error_class"]=typ if typ in ("AssertionError","KeyError","ValueError","TypeError","IndexError","StopIteration","FileNotFoundError","HTTPError","TimeoutError","JSONDecodeError","SystemExit","OSError") else "Other"
        trace=exc.__traceback__; lines=[]
        while trace is not None:
            fn=trace.tb_frame.f_code.co_filename
            if Path(fn).name=="receipt.py" and 0<trace.tb_lineno<=2000:lines.append(trace.tb_lineno)
            trace=trace.tb_next
        public["receipt_lines"]=lines[:12]
    finally:
        key=None
        marker=task/"submitted.json"
        if marker.is_file():
            public["invocation"]=str(UUID(json.loads(marker.read_text())["invocation_id"]))
        (root/"verdict.json").write_text(json.dumps(public,indent=2)+"\n")
    raise SystemExit(rc)
