from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

out = Path(sys.argv[1])
tmp = Path(os.environ["RUNNER_TEMP"])
record = {"source": os.environ["QUALIFICATION_COMMIT"], "exit": int(sys.argv[2]), "status": "FAIL"}
try:
    (stage,) = (tmp/"qualification").glob("invocation-*/events.jsonl")
    verdict = json.loads((stage.parent/"verdict.json").read_text())
    record["verdict"] = {k:verdict.get(k) for k in ("status","native_exit","capture_exit","expected","failures","missing","execution_finished_at")}
    events = [json.loads(line) for line in stage.read_text().splitlines()]
    record["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
    raw = subprocess.check_output([os.environ["BB_LOG_DECODER"],"print",f"--compact_execution_log={stage.parent/'execution.binpb'}","--raw=true","--sort=false"],text=True)
    decoder=json.JSONDecoder()
    pos=0
    while pos<len(raw):
        if raw[pos].isspace():
            pos+=1
            continue
        row,pos=decoder.raw_decode(raw,pos)
        action=row.get("spawn",{})
        if action.get("mnemonic")=="TestRunner" and action.get("targetLabel")=="//test/release:sdist_py37_install_qualification":
            props={p["name"]:p["value"] for p in action["platform"]["properties"]}
            record["action"]={"arch":props.get("Arch"), "image":props.get("container-image"), "runner":action.get("runner"),
                "cached":action.get("cacheHit",False),"exit":action.get("exitCode",0),"digest":action.get("digest")}
    log=Path("bazel-testlogs/test/release/sdist_py37_install_qualification/test.log").read_text(errors="replace")
    if record["exit"]!=0:
        record["diagnostics"]=log[-1850:]
    smoke=[json.loads(line.removeprefix("SDIST_INSTALL ")) for line in log.splitlines() if line.startswith("SDIST_INSTALL ")]
    record["installs"]=smoke
    a=record["action"]
    assert a["runner"]=="remote" and a["arch"]=="amd64" and a["image"].endswith("ea8897698c0955ba96144bd2b7310ef7884ccce4db7a1f97ffc21fb8b89d1673") and a["digest"] and not a["cached"],a
    assert record["exit"]==0 and verdict["status"]=="PASS" and a["exit"]==0,record["verdict"]
    assert len(smoke)==2 and all(s["python"]=="3.7.17" and s["modules"]==["__init__.py","sandbox.py"] for s in smoke),smoke
    expect=json.loads((out/"amd64.json").read_text())["files"]
    assert {s["artifact"]:s["sha256"] for s in smoke}==expect,smoke
    record["status"]="PASS"
except Exception as err:
    record["error"]=f"{type(err).__name__}: {err}"[:1200]
record["verified"]=datetime.now(timezone.utc).isoformat()
(out/"install.json").write_text(json.dumps(record,indent=2)+"\n")
print(record["status"],record.get("error",""))
raise SystemExit(record["status"]!="PASS")
