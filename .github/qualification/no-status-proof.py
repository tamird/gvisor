"""Exercise the release boundary from an exported source tree on a Linux worker."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

now = lambda: datetime.now(timezone.utc).isoformat()
tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "proof"
out.mkdir(exist_ok=True)
root = tmp / "archive-no-status"
root.mkdir()
source = os.environ["SOURCE"]
assert subprocess.check_output(["git","rev-parse","HEAD^1"],text=True).strip() == source
with subprocess.Popen(["git","archive",source],stdout=subprocess.PIPE) as p:
    with tarfile.open(fileobj=p.stdout,mode="r|") as archive:
        archive.extractall(root,filter="tar")
    assert p.wait()==0
assert not (root/".git").exists() and not (root/"tools/workspace_status.sh").exists()
assert subprocess.run(["git","-C",str(root),"rev-parse","--is-inside-work-tree"],capture_output=True).returncode
label = "release-20990101.7"
binary = "//runsc:runsc"
test = "//runsc:version_test"
pkg = "//tools/bazeldefs:version"
side = "//runsc/cmd/metricserver:runsc-metric-server"
targets = [binary,test,pkg,side]
expected = [f"{mode}/{key}" for mode in ("ordinary","release") for key in ("targets","TestVersion","cli","debian","no-policy","strict-empty","strict-other","strict-self")]
expected.extend(["publisher/master","publisher/staging"])
passed = []
attempted = []
errors = {}
reports = {}
guest = ["docker","exec","-w",str(root),"overlay-guest"]
def require(mode, key, ok, actual):
    attempted.append(f"{mode}/{key}")
    if not ok:
        errors[f"{mode}/{key}"] = str(actual)[:650]
        return False
    passed.append(f"{mode}/{key}")
    return True

def check(mode, flags, ver, deb):
    dest=out/mode
    dest.mkdir()
    bep=tmp/f"{mode}-events.json"
    cmd = ["bazel","--bazelrc="+str(tmp/"overlay.bazelrc"),"test","--config=x86_64","-c","opt","--jobs=18",
           "--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
           "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
           "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
           "--remote_download_outputs=toplevel","--nobuild_tests_only","--test_arg=-test.v","--test_output=errors",
           "--build_event_json_file="+str(bep),*flags,*targets]
    with (tmp/f"{mode}.log").open("wb") as stream:
        result=subprocess.run([*guest,*cmd],stdout=stream,stderr=subprocess.STDOUT)
    log=(tmp/f"{mode}.log").read_text(errors="replace")
    events=[json.loads(x) for x in bep.read_text().splitlines()] if bep.exists() else []
    completed={e["id"]["targetCompleted"]["label"]:e["completed"].get("success",False)
               for e in events if "targetCompleted" in e.get("id",{}) and "completed" in e}
    tests={e["id"]["testSummary"]["label"]:e["testSummary"].get("overallStatus") for e in events if "testSummary" in e}
    r=reports[mode]=dict(source=source,flags=flags,expected_targets=targets,completed=completed,
        failed_targets=[k for k,v in completed.items() if not v],missing_targets=sorted(set(targets)-completed.keys()),
        bazel_exit=result.returncode,bazel_finished=now(),invocations=sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[0-9a-f-]+",log))),test_summary=tests)
    if result.returncode:
        r["diagnostics"]=[s[:500] for s in log.splitlines() if ("ERROR:" in s or "FAIL:" in s)
                          and not re.search(r"(?i)(api[-_]key|secret|token|remote_header|bes_header)",s)][-10:]
    require(mode,"targets",result.returncode==0 and all(completed.get(t) for t in targets),r)
    # Keep the named test's own output so a successful zero-test binary cannot pass.
    readlog=subprocess.run([*guest,"cat","bazel-testlogs/runsc/version_test/test.log"],capture_output=True)
    raw=readlog.stdout if readlog.returncode==0 else b""
    (dest/"version-test.log").write_bytes(raw)
    r["named_test_log_exit"]=readlog.returncode
    r["skipped"]=[s.strip() for s in raw.decode(errors="replace").splitlines() if "--- SKIP:" in s]
    require(mode,"TestVersion",readlog.returncode==0 and tests.get(test)=="PASSED" and b"--- PASS: TestVersion " in raw and not r["skipped"],tests)
    line=subprocess.check_output([*guest,"bazel-bin/runsc/runsc_/runsc","--version"],text=True).splitlines()[0]
    r["cli_version"]=line
    require(mode,"cli",line=="runsc version "+ver,line)
    output=subprocess.check_output([*guest,"cat","bazel-bin/tools/bazeldefs/version.txt"],text=True)
    r["debian_version"]=output
    require(mode,"debian",output==deb+"\n",output)
    for key,want,reject in [("no-policy",None,False),("strict-empty","",True),("strict-other","different-release",True),("strict-self",ver,mode=="ordinary")]:
        env=[] if want is None else ["env","GVISOR_ENFORCE_RELEASE="+want]
        resp=subprocess.run([*guest,*env,"bazel-bin/runsc/cmd/metricserver/runsc-metric-server_/runsc-metric-server"],capture_output=True,text=True)
        mismatch="does not match the expected release" in resp.stderr and "panic:" in resp.stderr
        r[key]={"exit":resp.returncode,"mismatch":mismatch}
        require(mode,key,mismatch and resp.returncode not in (0,128) if reject else not mismatch and resp.returncode==128,r[key])
    r["execution_finished"]=now()

# Execute the Buildkite label-selection block without building or publishing
# in the release pipeline. Intercept only make. A closer staging tag must
# neither become an ordinary package's name nor obscure its prior release.
def check_publisher():
    text=(root/".buildkite/release.yaml").read_text()
    lines=text.splitlines()
    i,=[i for i,line in enumerate(lines) if 'if [[ "$${BUILDKITE_TAG:-}" == release-*-staging ]]' in line]
    j,=[j for j,line in enumerate(lines) if "make release-artifacts RELEASE_VERSION=" in line]
    script="\n".join(line[8:] for line in lines[i:j+1]).replace("$$","$")
    script='make() { printf "%s\\n" "$@"; };\n'+script
    repo=tmp/"publisher-fixture"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git","-c","user.name=Fixture","-c","user.email=fixture@example.invalid",*args],cwd=repo,text=True).strip()
    git("init","-q")
    git("commit","--allow-empty","-qm","release")
    git("tag","release-20990101.6")
    git("commit","--allow-empty","-qm","master")
    git("tag",label+"-staging")
    for name,tag,v in [("master","","release-20990101.6-1-g"+git("rev-parse","--short=12","HEAD")),("staging",label+"-staging",label)]:
        proc=subprocess.run(["bash","-eu","-o","pipefail","-c",script],cwd=repo,env=dict(os.environ,BUILDKITE_TAG=tag),capture_output=True,text=True,timeout=10)
        args=proc.stdout.splitlines()
        reports["publisher/"+name]={"exit":proc.returncode,"make_argv":args,"source":source}
        require("publisher",name,proc.returncode==0 and args==["release-artifacts","RELEASE_VERSION="+v,"BAZEL_OPTIONS=--compilation_mode=opt"],args)
try: check_publisher()
except Exception as e: errors["publisher"]=str(e)
for mode,flags,ver,deb in [("ordinary",[],"(devel)","0~devel"),("release",["--config=release_version","--embed_label="+label],label,"20990101.7")]:
    try:
        check(mode,flags,ver,deb)
    except Exception as exc:
        errors[mode]=str(exc)
    finished=now()
    verdict=dict(source=source,expected=expected,executed=attempted,passed_checks=passed,failed=errors,missing=sorted(set(expected)-set(attempted)),not_passed=sorted(set(expected)-set(passed)),reports=reports,
                 execution_finished=finished,verified_at=now(),passed=len(passed)==len(expected) and not errors)
    (out/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
    print(mode,errors.get(mode,"passed"),flush=True)
assert verdict["passed"],errors
