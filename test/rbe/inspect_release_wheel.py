"""Supplement the qualification harness with wheel execution and payload evidence."""
from datetime import datetime, timezone
from email.parser import BytesParser
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile

sys.path.insert(0, str(Path.cwd() / "test/rbe"))
from qualification_report import compact_records

root = Path(os.environ["RUNNER_TEMP"])
arch = os.environ["QUALIFICATION_ARCH"]
record = {"source": os.environ["GITHUB_SHA"], "architecture": arch, "expected": ["//sandboxexec/sandbox/python:dist"], "status": "FAIL"}
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
def missing_executables(log):
    tools = set()
    for path in re.findall(r"exec container process[^\n`]*`([^\n`]+)`: No such file or directory", log):
        name = path.replace("\\/", "/").rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_.+-]{1,100}", name) and not re.search("token|secret|api.?key",name,re.I):
            tools.add(name)
    return sorted(tools)

def audit():
    row = root / "qualification"
    assert (row / "final-head.txt").read_text().strip() == record["source"]
    stage, = row.glob("invocation-*/events.jsonl")
    verdict = json.loads((stage.parent / "verdict.json").read_text())
    record["harness"] = {k: verdict.get(k) for k in ("status", "native_exit", "capture_exit", "expected", "failures", "missing", "execution_finished_at")}
    events = [json.loads(s) for s in stage.read_text().splitlines()]
    record["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
    errors = [line for e in events for line in (e.get("progress",{}).get("stderr","")+"\n"+e.get("aborted",{}).get("description","")).splitlines()
              if re.search(r"(No such file|not found|ModuleNotFoundError|Error:|ERROR:|Exec format|Traceback|failed to start)",line,re.I)]
    record["diagnostic"] = [line[:500] for line in errors if not re.search(r"api.key|header|gh[pso]_|token|secret",line,re.I)][:20]
    spawns_file = root / "spawns.json"
    with spawns_file.open("w") as dest:
        subprocess.run([os.environ["BB_LOG_DECODER"], "print", f"--compact_execution_log={stage.parent/'execution.binpb'}", "--raw=true","--sort=false"],stdout=dest,check=True)
    _,spawns = compact_records(spawns_file)
    record["failed_actions"] = [{k:s.get(k) for k in ("mnemonic","targetLabel","runner","exitCode","status")} for s in spawns if s.get("exitCode") or s.get("status")][:20]
    actions = [s for s in spawns if s.get("mnemonic")=="PythonDistribution"]
    # Only publish tool basenames and action labels. Full coordinator output can contain credentials.
    coordinator = (root/"coordinator.log").read_text(errors="replace")
    record["missing_executables"] = missing_executables(coordinator)
    if "stat /tmp: no such file or directory" in coordinator:
        record["missing_system_tmp"] = True
    record["failed_actions"] = [
        {k:s.get(k) for k in ("mnemonic", "targetLabel", "exitCode")}
        for s in spawns if s.get("exitCode") or s.get("status")
    ][:20]
    record["distribution"] = [{k:s.get(k) for k in ("runner","cacheHit","exitCode","status","digest","targetLabel","platform")} for s in actions]
    assert record["harness"]["status"]=="PASS" and not record["harness"]["native_exit"] and not record["harness"]["capture_exit"]
    s,=actions
    props={p["name"]:p["value"] for p in s["platform"]["properties"]}
    assert s["runner"]=="remote" and s.get("digest") and not (s.get("cacheHit") or s.get("remoteCacheHit") or s.get("exitCode") or s.get("status")), s["runner"]
    assert props["Arch"]==arch and props["container-image"]==image, props
    version="2026.10.29.2"
    project=Path("sandboxexec/sandbox/python")
    p=Path("bazel-bin")/project/"dist"
    wheel,sdist = p/f"gvisor-{version}-py3-none-any.whl", p/f"gvisor-{version}.tar.gz"
    assert set(p.iterdir()) == {wheel,sdist}
    pkg = {str(f.relative_to(project)):f.read_bytes() for f in (project/"gvisor").glob("*.py")}
    assert pkg
    def metadata(data):
        msg=BytesParser().parsebytes(data)
        assert (msg["Name"],msg["Version"],msg["Requires-Python"]) == ("gvisor",version,">=3.7"), str(msg)
    with zipfile.ZipFile(wheel) as z:
        assert z.testzip() is None
        for name,v in pkg.items():
            assert z.read(name)==v
        prefix=f"gvisor-{version}.dist-info/"
        metadata(z.read(prefix+"METADATA"))
        for name,h,n in csv.reader(io.StringIO(z.read(prefix+"RECORD").decode())):
            if name==prefix+"RECORD":
                assert not h and not n
                continue
            data=z.read(name)
            assert h=="sha256="+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=") and int(n)==len(data)
    with tarfile.open(sdist,"r:gz") as t:
        def get(name): return t.extractfile(f"gvisor-{version}/{name}").read()
        metadata(get("PKG-INFO"))
        for name,v in pkg.items():
            assert get(name)==v
        assert get("README.md") == (project/"README.md").read_bytes()
    record["files"]={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (wheel,sdist)}
    record["source_modules"]=sorted(pkg)
    record["status"]="PASS"

try:
    audit()
except Exception as err:
    record["error"]=f"{type(err).__name__}: {err}"[:480]
finally:
    record["verified_at"]=datetime.now(timezone.utc).isoformat()
    receipt = Path(os.environ["PUBLICATION"])/f"{arch}.json"
    receipt.write_text(json.dumps(record,indent=2)+"\n")
    print(json.dumps({k:record.get(k) for k in ("source","architecture","status","error","invocation","diagnostic")}))
sys.exit(record["status"]!="PASS")
