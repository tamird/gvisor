"""Exercise production release frontends, metadata, reproducibility and a Python 3.7 consumer."""
import email
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
TEST = ROOT / "test/rbe/python_release_isolation"
SCRATCH = Path(os.environ["TEST_TMPDIR"])
OUTPUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
VERSION = "2026.10.29.2"
EPOCH = 315532800
MODULES = {"gvisor/__init__.py", "gvisor/sandbox.py"}
report = {}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as z:
            files = {name:z.read(name) for name in z.namelist()}
        metadata = next(v for k,v in files.items() if k.endswith(".dist-info/METADATA"))
        assert {k for k in files if k.startswith("gvisor/")} == MODULES,files.keys()
    else:
        with tarfile.open(path) as t:
            files = {m.name.partition("/")[2]:t.extractfile(m).read() for m in t if m.isfile()}
        metadata = files["PKG-INFO"]
        assert {"README.md", "pyproject.toml", *MODULES} <= files.keys(),files.keys()
    msg=email.message_from_bytes(metadata)
    return {"sha256":sha(path.read_bytes()), "members":sorted(files),
            "modules":{p:sha(files[p]) for p in sorted(MODULES)},
            "description":sha(str(msg.get_payload()).strip().encode()),
            "metadata":{k:sorted(msg.get_all(k,[])) for k in ("Name","Version","Summary","Requires-Python","License","License-Expression","Classifier","Project-URL","Requires-Dist","Description-Content-Type")}}


def build(backend, frontend, date):
    work=SCRATCH / backend / frontend
    proj=work / "sandboxexec/sandbox/python"
    shutil.copytree(ROOT / "sandboxexec/sandbox/python",proj,symlinks=False)
    if backend != "stock":
        shutil.copyfile(TEST / (backend+".toml"),proj / "pyproject.toml")
    for file in proj.rglob("*"):
        if file.is_file(): os.utime(file,(date,date))
    tools=work/"tools"; tools.mkdir()
    entry=tools/"make_python_release.sh"; shutil.copy(TEST/"fixed.sh",entry)
    out=work/"dist"; temp=work/"temp"; temp.mkdir()
    env=dict(os.environ,TMPDIR=str(temp),SOURCE_DATE_EPOCH=str(EPOCH))
    if frontend=="uv":
        shim=work/"bin"; shim.mkdir()
        script=shim/"python3"; script.write_text("#!/bin/sh\nexit 1\n");script.chmod(0o755)
        env["PATH"]=str(shim)+os.pathsep+env["PATH"]
    result=subprocess.run(["bash",str(entry),"build",str(out),"release-20261029.2"],env=env,text=True,capture_output=True)
    if result.returncode:
        raise RuntimeError((result.stdout+result.stderr)[-2200:])
    assert not list(temp.iterdir()), list(temp.iterdir())
    marker="Successfully built wheel and sdist with python3 -m build." if frontend=="build" else "python3 -m build not available, building with uv build..."
    assert marker in result.stdout,result.stdout[-1000:]
    artifacts=sorted(out.iterdir())
    assert len(artifacts)==2 and {a.name for a in artifacts}=={f"gvisor-{VERSION}-py3-none-any.whl", f"gvisor-{VERSION}.tar.gz"}
    return {a.name:inspect(a) for a in artifacts}, artifacts


def install37(path, dest):
    dest.mkdir()
    python=os.environ["PACKAGE_PY37"]
    proc=subprocess.run([python,"-m","pip","install","--isolated","--no-index","--no-deps","--find-links",os.environ["PIP_FIND_LINKS"],"--target",str(dest),str(path)],text=True,capture_output=True)
    if proc.returncode: raise RuntimeError((proc.stdout+proc.stderr)[-1800:])
    prog="import gvisor.sandbox,sys; print(sys.version.split()[0]); print(gvisor.sandbox.__file__)"
    actual=subprocess.check_output([python,"-c",prog],env=dict(os.environ,PYTHONPATH=str(dest)),cwd=dest,text=True).splitlines()
    assert actual[0]=="3.7.17" and Path(actual[1]).is_relative_to(dest),actual
    return {"python":actual[0], "modules":{p:sha((dest/p).read_bytes()) for p in sorted(MODULES)}}


for backend in ("stock","flit","hatch"):
    data=report[backend]={}
    try:
        artifacts=[]
        for frontend,date in (("build",946684800),("uv",1609459200)):
            values,artifacts=build(backend,frontend,date);data[frontend]=values
        data["pair_identical"]=all(val["sha256"]==data["uv"][name]["sha256"] for name,val in data["build"].items())
        if backend!="stock":
            data["installs"]={a.name:install37(a,SCRATCH/backend/("install-"+a.name)) for a in artifacts}
            data["metadata_changes"]={}
            for n,v in data["build"].items():
                baseline=report["stock"]["build"][n]
                assert v["modules"]==baseline["modules"]==data["installs"][n]["modules"]
                assert v["description"]==baseline["description"]
                diffs={k:[b,v["metadata"][k]] for k,b in baseline["metadata"].items() if b!=v["metadata"][k]}
                data["metadata_changes"][n]=diffs
            assert data["pair_identical"]
        data["status"]="PASS"
    except Exception as e:
        data.update(status="FAIL",error=f"{type(e).__name__}: {e}")
    print(backend,data["status"],data.get("error",""),flush=True)
    (OUTPUT/"backend-results.json").write_text(json.dumps(report,indent=2)+"\n")
assert report["stock"]["status"]=="PASS" and any(report[b]["status"]=="PASS" for b in ("flit","hatch"))
