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

SOURCE = "01446201fd8e758267f4e1abd9727926655110b7"
CANDIDATE = "1bf40ca861c2eafd24aa4d8d8d1989a8e4788e87"
REQUEST = "70d0fa39b4e0eed99e9a097a10129176c7d66bb4023396cf2b8c81145061301d"
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

def verdict(task):
    r=json.loads((task/"receipt.json").read_bytes())
    assert r["sources"]==r["expected_sources"]==[SOURCE] and r["native_invocation"]["invocationId"]==json.loads((task/"submitted.json").read_bytes())["invocation_id"]
    result={"source":SOURCE,"candidate":CANDIDATE,"request":REQUEST,"invocation":str(UUID(r["native_invocation"]["invocationId"])),"status":enum(r["status"]),"producer":enum(r["producer_status"]),"capture":enum(r["collection_status"])}
    if r["collection_status"]!="COMPLETE":
        return result
    f=task/"qualification.tar.gz"
    assert f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest()==r["artifact"]["sha256"]
    result["archive"]={"sha256":digest(r["artifact"]["sha256"]),"bytes":number(r["artifact"]["bytes"])}
    with tarfile.open(f) as t:
        stock=json.load(t.extractfile("supplier-stock-writers.json"))
        v=json.load(t.extractfile("invocation-supplier-tests/verdict.json"))
        rows=[json.loads(x) for x in t.extractfile("invocation-supplier-tests/events.jsonl")]
        [child]=[x["started"]["uuid"] for x in rows if "started" in x.get("id",{})]
    assert stock["source"]==v["source"]==SOURCE
    result["child"]=str(UUID(child))
    result["native"]={"status":enum(v["status"]),"exit":number(v["native_exit"],255)}
    if v.get("bep_finish",{}).get("finishTime") and r.get("consumer_verified_at"):
        delta=(datetime.fromisoformat(r["consumer_verified_at"])-datetime.fromisoformat(v["bep_finish"]["finishTime"].replace("Z","+00:00"))).total_seconds()
        if 0<=delta<86400:result["native_finish_to_verified_ms"]=round(delta*1000,3)
    result["comparison"]=enum(stock.get("status"))
    result["owners"]={}
    for label,count in OWNERS.items():
        if label not in stock["owners"]:continue
        x=stock["owners"][label];assert x["cases"]==count
        reports={row["name"]:{"sha256":digest(row["sha256"]),"bytes":number(row["bytes"])} for row in x["reports"] if row["name"] in ("test.xml","test.log")}
        assert set(reports)=={"test.log","test.xml"}
        result["owners"][label]={"status":enum(x["status"]),"cases":count,"failed":number(x["failed_cases"],count),"action":digest(x["action"]["hash"]),"primary":reports}
    result["writers"]={}
    for label,names in PRODUCTS.items():
        if label not in stock["writers"]:continue
        x=stock["writers"][label];header=bytes.fromhex(x["entry_header"])
        outputs={}
        for e in x["outputs"]:
            [kind]=[n for n in names if e["path"].endswith("."+n)]
            assert kind not in outputs
            outputs[kind]={"sha256":digest(e["digest"]["hash"]),"bytes":number(int(e["digest"]["sizeBytes"]))}
        assert set(outputs)==set(names)
        result["writers"][label]={"action":digest(x["action"]["hash"]),"entry_sha256":digest(x["launcher"][-1]["digest"]["hash"]),"entry_class":("env-bash" if header in (b"#!/usr/bin/env bash",b"#!/usr/bin/env -S bash") else "script" if header.startswith(b"#!") else "ELF" if header.startswith(b"\x7fELF") else "other"),"outputs":outputs,"inputs":number(x["input_count"])}
    m=task/"original-action-mode.json"
    if m.exists():
        raw=json.loads(m.read_bytes())
        z=result["input_mode"]={"status":enum(raw["status"])}
        if raw["status"]=="COMPLETE":
            assert raw["source"]==SOURCE and str(UUID(raw["native_parent"]))==result["invocation"]
            z.update(action=digest(raw["action"][0]),input_sha256=digest(raw["hello_input"]["digest"][0]),input_bytes=number(raw["hello_input"]["digest"][1]),executable=raw["hello_input"]["is_executable"] is True,header_mode=number(int(raw["member"]["mode"],8),0o7777),same_input_mode=raw["same_input_mode"] is True,same_header_mode=raw["same_header_mode"] is True)
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
    root=Path(os.environ["RUNNER_TEMP"])/"supplier-stock"
    task=root/"task"/"job"
    sys.path.insert(0,str(task))
    public={"source":SOURCE,"candidate":CANDIDATE,"request":REQUEST,"stage":"submit"}
    rc=1
    try:
        with redirect_stdout(Quiet()),redirect_stderr(Quiet()):
            submit=runpy.run_path(str(task/"submit.py"))
            rc=submit["main"](lambda _:key)
        key=None
        public["stage"]="reconcile"
        public=verdict(task)
    except BaseException:
        rc=1
    finally:
        key=None
        marker=task/"submitted.json"
        if marker.is_file():
            public["invocation"]=str(UUID(json.loads(marker.read_text())["invocation_id"]))
        (root/"verdict.json").write_text(json.dumps(public,indent=2)+"\n")
    raise SystemExit(rc)
