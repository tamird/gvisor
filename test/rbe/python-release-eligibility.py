"""Exercise the release job's command and its publisher without external publication."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from zipfile import ZipFile

repo = Path.cwd()
out = Path(os.environ["RUNNER_TEMP"]) / "release-eligibility"
out.mkdir()
results = {"completed": [], "failures": [], "expected": [], "archives": []}
def run(argv, **kwargs):
    return subprocess.run(argv, text=True, check=True, capture_output=True, **kwargs).stdout
def git(*args):
    return run(["git", *args]).strip()
def write(path, data):
    path.write_text(data)
    path.chmod(0o755)
def timestamp():
    return datetime.now(timezone.utc).isoformat()
def check(name, func):
    results["expected"].append(name)
    try:
        detail = func()
        results["completed"].append({"name":name,"observation":detail})
    except Exception as err:
        results["failures"].append({"name":name, "error":str(err), "stderr":getattr(err,"stderr","")[-1400:]})
    (out/"verdict.json").write_text(json.dumps(results,indent=2)+"\n")

control, product = os.environ["CONTROL"], os.environ["PRODUCT"]
assert git("rev-parse","HEAD^") == product
ruby = 'require "yaml";require "json";puts JSON.generate(YAML.safe_load(STDIN.read, aliases: true)["steps"].find{|x| x["label"]==":ship: Release"}["commands"])'
commands = {}
for kind, ref in [("stock",control), ("candidate",product)]:
    steps = json.loads(run(["ruby","-e",ruby],input=git("show",ref+":.buildkite/release.yaml")))
    i, = [i for i,s in enumerate(steps) if "make artifacts-python" in s or "tools/make_release.sh build-python" in s]
    assert steps.index("./.buildkite/scripts/release/tag.sh stage") < i < next(i for i,s in enumerate(steps) if "make release RELEASE_NIGHTLY" in s)
    commands[kind] = steps[i].replace("$$","$")
publishers = {}
for kind, rev in [("stock", control), ("candidate", product)]:
    path = out / ("publisher-" + kind)
    write(path, git("show", rev+":tools/make_release.sh")+"\n")
    publishers[kind] = path
assert git("ls-tree",product,"tools/make_release.sh").startswith("100755 ")
archive_names = ["gvisor-2026.10.31.9-py3-none-any.whl","gvisor-2026.10.31.9.tar.gz"]
bare=out/"local-upstream.git"
run(["git","init","--bare",str(bare)])
git("--git-dir",str(bare),"config","receive.shallowUpdate","true")
git("config","user.name","Release Test")
git("config","user.email","test@example.invalid")
git("config","url."+bare.as_uri()+".insteadOf","https://github.com/google/gvisor.git")
shim = out/"shim"
shim.mkdir()
write(shim/"make",'#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
repo_tag="release-20261031.9"
stage=repo_tag+"-staging"
def reset_tags():
    tags=git("tag","--list").splitlines()
    if tags:git("tag","-d",*tags)
def label(ref, annotated=False):
    if annotated: git("tag","-a",ref,"-m","release fixture","HEAD")
    else: git("tag",ref,"HEAD")
cases=[
    ("untagged-master", [],"",False),
    ("only-ancestor", [],"",False),
    ("nightly", [],"true",False),
    ("nightly-with-release", [repo_tag],"true",False),
    ("test-tag", ["buildkite-test-branch"],"",False),
    ("staging-only", [stage],"",False),
    ("tagged-master", [repo_tag],"",True),
    ("mixed-tags", [stage,"buildkite-test-branch",repo_tag],"",True),
    ("two-versioned-tags", [repo_tag,"release-20261030.8"],"",True),
    ("after-staging", [stage],"",True),
]
def scenario(name,tags,nightly,want):
    reset_tags()
    for tag in tags: label(tag,annotated=name=="after-staging")
    if name=="only-ancestor":git("tag","release-20261030.0","HEAD^")
    if name=="after-staging":
        git("push",bare.as_uri(),"refs/tags/"+stage)
        run(["bash",".buildkite/scripts/release/tag.sh","stage"],env=dict(os.environ,BUILDKITE_TAG=stage))
        assert git("tag","--points-at","HEAD").splitlines() == [repo_tag]
    path=out/name;path.mkdir()
    counts={}
    for kind,command in commands.items():
        calls=path/(kind+".calls")
        env=dict(os.environ,PATH=str(shim)+":"+os.environ["PATH"],CALLS=str(calls),RELEASE_NIGHTLY=nightly,BUILDKITE_TAG=stage if name=="after-staging" else "")
        run(["bash","-euo","pipefail","-c",command],env=env)
        args=calls.read_text().splitlines() if calls.exists() else []
        assert args == (["artifacts-python"] if kind=="stock" or want else []),(name,kind,args)
        counts[kind]=len(args)
    # Run original raw publication; bypass only APT signing, external AR upload and network.
    tools=path/"tools";tools.mkdir()
    write(tools/"make_apt.sh", '#!/bin/sh\nexit 0\n')
    write(tools/"make_python_release.sh", '#!/bin/sh\nset -eu\ntest "$1" = upload-wheel\ntest -f "$2/gvisor-2026.10.31.9-py3-none-any.whl"\ntest -f "$2/gvisor-2026.10.31.9.tar.gz"\nprintf "%s\\n" "$2" >> "$UPLOADS"\n')
    inputs=[]
    for f in archive_names:
        a=path/f;a.write_text("eligibility input"); inputs.append(str(a))
    manifests = {}
    for kind, script in publishers.items():
        dest=path/("published-"+kind); dest.mkdir()
        uploads=path/("uploads-"+kind)
        env=dict(os.environ,GIT_DIR=str(repo/".git"),GIT_WORK_TREE=str(repo),NIGHTLY=nightly,UPLOADS=str(uploads))
        run(["bash",str(script),"unused.key",str(dest),"/bin/true",*inputs],cwd=path,env=env)
        files=sorted(str(f.relative_to(dest)) for f in dest.rglob("*") if f.is_file())
        wheels=[x for x in files if x.endswith(".whl")]
        assert bool(wheels)==want and uploads.exists()==want,(name,kind,wheels)
        if name == "two-versioned-tags":
            assert len(uploads.read_text().splitlines())==2
        manifests[kind]=files
    assert manifests["stock"]==manifests["candidate"], name
    return {"packaging_calls":counts,"files":len(files),"publisher_wheels":wheels}


for name,tags,nightly,want in cases:
    check(name,lambda:scenario(name,tags,nightly,want))

def broken_git():
    reset_tags()
    calls=out/"bad-git.calls"
    env=dict(os.environ,PATH=str(shim)+":"+os.environ["PATH"],CALLS=str(calls),
        GIT_DIR=str(out/"nonexistent.git"),RELEASE_NIGHTLY="false")
    result=subprocess.run(["bash","-euo","pipefail","-c",commands["candidate"]],env=env,capture_output=True,text=True)
    assert result.returncode and not calls.exists() and "not a git repository" in result.stderr
    return {"exit":result.returncode,"builder_calls":0,"git_failure_visible":True}
check("git-enumeration-error",broken_git)

def build():
    reset_tags();label(repo_tag)
    env=dict(os.environ,RELEASE_NIGHTLY="false",MAKEFLAGS="RELEASE_NAME="+repo_tag)
    with (out/"real-build.log").open("w") as log:
        proc=subprocess.run(["bash","-euo","pipefail","-c",commands["candidate"]],text=True,stdout=log,stderr=subprocess.STDOUT,env=env)
    if proc.returncode:
        raise RuntimeError((out/"real-build.log").read_text()[-1600:])
    dist=repo/"artifacts/python"
    names=sorted(f.name for f in dist.iterdir())
    assert names==sorted(archive_names),names
    with ZipFile(dist/archive_names[0]) as z:
        metadata=z.read(next(s for s in z.namelist() if s.endswith(".dist-info/METADATA"))).decode()
        assert "Version: 2026.10.31.9" in metadata
    with tarfile.open(dist/archive_names[1]) as t:
        metadata=t.extractfile("gvisor-2026.10.31.9/PKG-INFO").read().decode()
        assert "Version: 2026.10.31.9" in metadata
    return [{"name":n,"size":(dist/n).stat().st_size,"sha256":hashlib.sha256((dist/n).read_bytes()).hexdigest()} for n in names]
check("actual-tagged-builder-and-distributions",build)
results["finished"]=timestamp()
results["missing"]=sorted(set(results["expected"])-{r["name"] for r in results["completed"]}-{r["name"] for r in results["failures"]})
results["verified"]=not results["failures"] and not results["missing"] and len(results["completed"])==12
(out/"verdict.json").write_text(json.dumps(results,indent=2)+"\n")
print(json.dumps(results))
raise SystemExit(not results["verified"])
