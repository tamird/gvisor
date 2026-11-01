import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone
from zipfile import ZipFile

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "flit-evidence"
out.mkdir(exist_ok=True)
source = os.environ["PROOF"]
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
package = "//sandboxexec/sandbox/python"
results = dict(carrier=os.environ['GITHUB_SHA'], source=source, product=os.environ['PRODUCT'], worker="ordinary BuildBuddy Linux execution platform",
               frontend="https://github.com/tamird/gvisor/actions/runs/41183200050/artifacts/13035474364", arms={})
def now():
    return datetime.now(timezone.utc).isoformat()
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def save():
    (out/"results.json").write_text(json.dumps(results,indent=2)+"\n")
original = tmp/"frontend.zip"
assert digest(original)=="4bb2c64b208e62937e40c7cfbc65b5a6fadd011e131f0713f054c7c8e8f93a2a"
with ZipFile(original) as z:
    expected=json.loads(z.read("test.outputs/backend-results.json"))['flit']
assert expected['status']=='PASS' and expected['pair_identical']
assert expected['build']==expected['uv'] and all(x['python']=='3.7.17' for x in expected['installs'].values())
results['baseline']={k:v['sha256'] for k,v in expected['build'].items()}
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
from io import BytesIO
from urllib.request import urlopen
package="//sandboxexec/sandbox/python:dist"
settings=["--config=x86_64","--lockfile_mode=off","--//sandboxexec/sandbox/python:dist_version=2026.10.29.2"]
bz=["bazel","--bazelrc="+str(tmp/'sidecar.bazelrc')]
env={**os.environ,"DOCKER_BUILD":"false","DOCKER_CLI_PATH":"/bin/false"}
def run(name,argv,readout=None):
    a={"args":argv,"started":now()};results["arms"][name]=a;save()
    try:
        proc=subprocess.run(["timeout","--signal=INT","--kill-after=12s","13m",*argv],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        a.update(native_exit=proc.returncode,finished=now(),invocations=re.findall(r"Invocation ID: ([0-9a-f-]{36})",proc.stdout))
        a["diagnostics"]=[l[:700] for l in proc.stdout.splitlines() if re.search(r"ERROR|FAILED|licensecheck:|cannot |stale entry|unknown |extraneous|not found",l,re.I) and not re.search(r"remote_header|api.?key|credential|authorization|bes_header",l,re.I)][-25:]
        assert proc.returncode==0
        if readout:readout(a,proc.stdout)
        a["verdict"]="PASS"
    except Exception as e:a.update(verdict="INCOMPLETE",error=repr(e)[:500])
    save()
def outputs(a,stdout):
    target=Path("bazel-bin/sandboxexec/sandbox/python/dist")
    a["files"]={f.name:digest(f) for f in target.iterdir()}
    assert a["files"]==results["baseline"]
def runtime(a,stdout):
    args=[x.strip() for x in stdout.splitlines() if "bin/python3" in x]
    assert args and not any("protoc_check_static_python" in x for x in args)
    a["interpreter"]=args[0][:450]
run("registered-runtime",[*bz,"aquery",*settings,'mnemonic("SandboxExecDist", '+package+')'],runtime)
run("ordinary-artifacts",[*bz,"build",*settings,package],outputs)
# The checker shells out to the repository's ordinary make mod target.
# Run it through make so its nested invocation inherits the no-Docker flags.
run("canonical-license",["make","DOCKER_BUILD=false","DOCKER_CLI_PATH=/bin/false","STARTUP_OPTIONS=--bazelrc="+str(tmp/'sidecar.bazelrc'),"run","TARGETS=//tools/licensecheck/main:licensecheck","ARGS=--mode=verify"])
a=results["arms"]["notice-files"]={"started":now()}
try:
    wheel="https://files.pythonhosted.org/packages/f2/65/b6ba90634c984a4fcc02c7e3afe523fef500c4980fec67cc27536ee50acf/flit_core-3.12.0-py3-none-any.whl"
    with urlopen(wheel,timeout=20) as r:content=r.read()
    a["wheel_sha256"]=hashlib.sha256(content).hexdigest()
    assert a["wheel_sha256"]=="e7a0304069ea895172e3c7bb703292e992c5d1555dd1233ab7b5621b5b69e62c"
    with ZipFile(BytesIO(content)) as z:
        paths=sorted(n for n in z.namelist() if n.endswith("/LICENSE"))
        assert len(paths)==2,paths
        a["notices"]={}
        for n in paths:
            b=z.read(n)
            license="MIT" if b"Permission is hereby granted" in b else "BSD-3-Clause" if b"Neither the name" in b or b"Neither the name".lower() in b.lower() else None
            assert license
            a["notices"][n]={"license":license,"sha256":hashlib.sha256(b).hexdigest()}
        assert {v["license"] for v in a["notices"].values()}=={"MIT","BSD-3-Clause"}
    a["verdict"]="PASS"
except Exception as e:a.update(verdict="INCOMPLETE",error=repr(e))
a['finished']=now();results['verified']=now();save()
print({k:(v["verdict"],v.get("diagnostics"),v.get("error")) for k,v in results["arms"].items()},flush=True)
assert all(v["verdict"]=="PASS" for v in results["arms"].values())
