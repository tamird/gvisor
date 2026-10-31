"""Reconcile three native kernel saves, their UDP reads, and the graph census."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

def now():
    return datetime.now(timezone.utc).isoformat()

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "proof"
out.mkdir(exist_ok=True)
source = os.environ["SOURCE"]
assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == source
label = "//runsc/container:container_test"
arms = {"empty": (0, 64), "short": (12, 64), "large": (12, 8192)}
verdict = dict(source=source, parent=subprocess.check_output(["git","rev-parse","HEAD^^"],text=True).strip(), expected=list(arms), attempted=[], passed=[], missing=list(arms), skipped=[], failures={}, measurements={})
def save():
    (out/"verdict.json").write_text(json.dumps(verdict, indent=2)+"\n")
save()
subprocess.run(["git","checkout","--quiet","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
shell = ["docker","exec","-e","GIT_CONFIG_COUNT=1","-e","GIT_CONFIG_KEY_0=safe.directory","-e","GIT_CONFIG_VALUE_0="+os.environ["GITHUB_WORKSPACE"],"overlay-guest"]
bep = tmp/"graph-events.json"
work = tmp/"measure"
work.mkdir()
cmd=["bazel","--bazelrc="+str(tmp/"overlay.bazelrc"),"test","--config=x86_64","-c","opt","--jobs=16",
     "--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
     "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
     "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
     "--strategy=TestRunner=standalone","--test_sharding_strategy=disabled","--test_filter=^TestGraphUDPIndex$","--test_arg=-test.v",
     "--cache_test_results=no","--test_timeout=450","--zip_undeclared_test_outputs","--remote_download_outputs=toplevel","--test_output=errors","--build_event_json_file="+str(bep),label]
with (work/"bazel.log").open("wb") as stream:
    bazel=subprocess.run(shell+cmd,stderr=subprocess.STDOUT,stdout=stream)
verdict["execution_finished"]=now()
verdict["bazel_exit"]=bazel.returncode
log=(work/"bazel.log").read_text(errors="replace")
verdict["invocations"]=sorted(set(re.findall(r'https://app.buildbuddy.io/invocation/[\da-f-]+',log)))
verdict["target_status"]={}
if bep.exists():
    for line in bep.open():
        event=json.loads(line)
        tid=event["id"].get("testSummary",{}).get("label")
        if tid:
            verdict["target_status"][tid]=event["testSummary"]["overallStatus"]
def collect(name):
    path="bazel-testlogs/runsc/container/container_test/"+name
    outpath=work/Path(name).name
    with outpath.open("wb") as stream:
        rc=subprocess.run(shell+["cat",path],stdout=stream,stderr=subprocess.DEVNULL).returncode
    return outpath if rc==0 else None
testlog=collect("test.log")
result_lines=testlog.read_text(errors="replace").splitlines() if testlog else []
def safe(lines):
    return [x[:1400] for x in lines if not re.search(r'api[-_ ]?key|secret|token|remote_header|bes_header|authorization:', x, re.I)]
(out/"test-evidence.txt").write_text("\n".join(safe(x for x in result_lines if any(t in x for t in ["=== RUN","--- PASS:","--- SKIP:","--- FAIL:","UDP restored","failed:","timed out","panic:","FAIL:"])))+"\n")
if bazel.returncode:
    (out/"diagnostics.txt").write_text("\n".join(safe(x for x in log.splitlines() if "ERROR:" in x or "FAIL:" in x or "undefined:" in x or ".go:" in x))[-10000:]+"\n")
archive=collect("test.outputs/outputs.zip")
for arm,(count,size) in arms.items():
    name="TestGraphUDPIndex/"+arm
    if any("=== RUN   "+name in x for x in result_lines): verdict["attempted"].append(arm)
    if any("--- SKIP: "+name+" " in x for x in result_lines): verdict["skipped"].append(arm)
    records=[]
    if archive is not None and zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for path in bundle.namelist():
                if not path.endswith("."+arm): continue
                for line in bundle.read(path).decode(errors="replace").splitlines():
                    if line.startswith("{"):
                        try: line=json.loads(line).get("msg",line)
                        except ValueError: pass
                    match=re.search(r'Kernel save stats:.*?INDEX_CENSUS=(\{[^{}]+\})',line)
                    if match: records.append({"log":path,"counts":json.loads(match[1])})
    verdict["measurements"][arm]=records
    clean=any("--- PASS: "+name+" " in x for x in result_lines)
    after=any(f"UDP restored count={count} bytes={size}" in x for x in result_lines)
    complete=len(records)==1 and records[0]["counts"]["keys"]>0 and records[0]["counts"]["entries"]>=records[0]["counts"]["keys"]
    if clean and after and complete: verdict["passed"].append(arm)
    else: verdict["failures"][arm]={"named_pass":clean,"payload_verified":after,"census_records":len(records)}
verdict["missing"]=sorted(set(arms)-set(verdict["attempted"]))
verdict["verified_at"]=now()
verdict["ok"]=(bazel.returncode==0 and verdict["target_status"]=={label:"PASSED"} and verdict["passed"]==list(arms) and not verdict["skipped"])
save()
print(json.dumps(verdict,indent=2))
if not verdict["ok"]: raise SystemExit(1)
