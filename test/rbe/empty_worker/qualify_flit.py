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
results = dict(carrier=os.environ['GITHUB_SHA'], source=source, product=os.environ['PRODUCT'], worker=image,
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
subprocess.run(["git","checkout","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
previous=tmp/"scratch.zip"
assert digest(previous)=="9423eae1c9911121f9e2ccae65db8d0e6c9ac6e6829397744c40a1e00786efba"
with ZipFile(previous) as z:
    arms = json.loads(z.read("results.json"))["arms"]
assert all(a["native_exit"]==0 and a["files"]==results["baseline"] for a in arms.values())
assert arms["amd64"]["make"]["native_exit"]==2 and "malformed expression" in arms["amd64"]["make"]["errors"][0]
results["original"]={"url":"https://github.com/tamird/gvisor/actions/runs/41262122405/artifacts/13065846593","sha256":digest(previous),"arms":arms}
stage=tmp/"staged-python"
platform="//test/rbe/empty_worker:amd64"
options=" ".join(["--config=x86_64","--lockfile_mode=off","--host_platform="+platform,"--extra_execution_platforms="+platform,"--extra_toolchains=//test/rbe/protoc_check:static_python"])
a=["make","DOCKER_BUILD=false","DOCKER_CLI_PATH=/bin/false","STARTUP_OPTIONS=--bazelrc="+str(tmp/'sidecar.bazelrc'),"BAZEL_OPTIONS="+options,"PYTHON_RELEASE_DEST="+str(stage),"RELEASE_NAME=release-20261029.2","artifacts-python"]
try:
    results["make"]={"started":now(),"args":a}
    m=subprocess.run(["timeout","--signal=INT","--kill-after=20s","16m",*a],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    err=[]
    for l in m.stdout.splitlines():
        if re.search(r"error|not found|failed|cquery|out of range|no such|cp:|release-20261029",l,re.I) and not re.search(r"api.?key|authorization|credential|remote_header",l,re.I):
            err.append(l[:900])
    results["make"].update(native_exit=m.returncode,finished=now(),invocations=re.findall(r"Invocation ID: ([0-9a-f-]{36})",m.stdout),diagnostics=err[-45:])
    if stage.is_dir():results["make"]["files"]={f.name:digest(f) for f in stage.iterdir()}
    assert m.returncode==0 and results["make"]["files"]==results["baseline"]
    results["verdict"]="DOCKER_FREE_MAKE_STAGES_BOTH_ORIGINAL_ARCHIVES"
finally:
    results['verified']=now();save();print(results.get('verdict'),results['make'],flush=True)
