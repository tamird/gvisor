import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone

source = Path.cwd()
head = subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()
assert head == "0eb8eca5ba25161133cd6fb8ad5a21604e988b92",head
module = source / "MODULE.bazel"
original = module.read_text()
base_version = re.search(r'bazel_dep\(name = "platforms", version = "([^"]+)"',original)
assert base_version
rows = {}
def run(name, verb, *args):
    if verb == "query":
        opts=[]
    else:
        opts=["--platforms=@platforms//host"]
    cmd=["bazel","--ignore_all_rc_files",verb,"--ignore_dev_dependency","--override_module=gvisor="+str(source),*opts,*args]
    p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    details=[line for line in p.stderr.splitlines() if any(x in line for x in ("ERROR:", "is forbidden","No repository visible","does not exist","not declared","not visible","unknown repo"))]
    rows[name]={"exit":p.returncode,"errors":details[-4:],"finished_at":datetime.now(timezone.utc).isoformat()}
    print("CASE",name,p.returncode,flush=True)
    return p

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    Path("MODULE.bazel").write_text('module(name="gazelle_consumer")\nbazel_dep(name="gvisor", version="0.0.1")\nbazel_dep(name="platforms", version="%s")\n' % base_version[1])
    Path("BUILD.bazel").touch()
    Path(".bazelversion").write_bytes((source/".bazelversion").read_bytes())
    for name,args in {"resources_wildcard":["@gvisor//test/kubernetes/benchmarks/resources:all"],
                      "sentry_package_group":["@gvisor//pkg/sentry:internal"]}.items():
        run(name,"build",*args)
    # Diagnostic only: Gazelle rejects a non-root declaration of these ten
    # overrides. Mask those tags to measure wildcard selection on the actual
    # candidate. This is neither an endorsement nor a proposed source change.
    override = 'root_go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps", dev_dependency = True)\n'
    assert original.count("go_deps.gazelle_override(")==9 and original.count("go_deps.module_override(")==1
    amended=original.replace("go_deps.gazelle_override(","root_go_deps.gazelle_override(").replace("go_deps.module_override(",override+"root_go_deps.module_override(",1)
    try:
        module.write_text(amended)
        labels=run("isolated_root_labels","query","@gvisor//:all")
        if labels.returncode==0:
            names=[s.rsplit(":",1)[1] for s in labels.stdout.splitlines() if ":" in s and " " not in s]
            assert "license" in names
            drop=["-@gvisor//:"+n for n in names if n not in ("license","gazelle_check")]
            run("isolated_explicit_license","build","@gvisor//:license")
            run("isolated_root_wildcard","build","--","@gvisor//:all",*drop)
            run("isolated_explicit_check","build","--nobuild","@gvisor//:gazelle_check")
    finally:
        module.write_text(original)
    os.chdir(source)
expected=("resources_wildcard","sentry_package_group","isolated_explicit_license","isolated_root_wildcard")
ok=all(rows.get(key,{}).get("exit")==0 for key in expected)
failure=rows.get("isolated_explicit_check",{})
ok=ok and failure.get("exit",0)!=0 and any("lint_inputs" in s or "go_repository_config" in s for s in failure.get("errors",[]))
record={"source":head,"status":"PASS" if ok else "LIMITED","rows":rows,"finished_at":datetime.now(timezone.utc).isoformat()}
print("GAZELLE_CONSUMER_RESULT",json.dumps(record,separators=(",",":")),flush=True)
record["retained_controls"]="https://github.com/tamird/gvisor/actions/runs/41211321121/artifacts/13046890088"
record["actual_consumer"]="https://github.com/tamird/gvisor/actions/runs/41211749632/artifacts/13045764820"
if d:=os.environ.get("RUNNER_TEMP"):
    out=Path(d)/"gazelle-qualification"/"verdict.json"
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(record,indent=2))
raise SystemExit(0 if ok else 1)
