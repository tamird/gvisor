"""Verify the actual benchmark and publish only the requested profiles and counters."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from zipfile import ZipFile

workspace = Path(os.environ["GITHUB_WORKSPACE"])
tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp/"profile-proof"
out.mkdir(exist_ok=True)
source = os.environ["SOURCE"]
target = "//test/rbe/graph_profile:nginx"
results = {"source":source, "upstream":"6e6276344dce3728a09a8271c8360269e7d881a4", "benchmark_pr":"0e53bb437a08531aa1220066f123c1a2ec827fe6", "target":target, "execution_started":datetime.now(timezone.utc).isoformat(), "expected":[a+"-"+b for a in ("Decode","Encode") for b in ("ordinary1","ordinary2","ordinary3","cpu","alloc")], "observed":[], "failures":[], "skips":[], "missing":[]}
def emit():
    results["missing"]=sorted(set(results["expected"])-set(results["observed"]))
    (out/"verdict.json").write_text(json.dumps(results,indent=2)+"\n")
emit()
subprocess.run(["git","checkout","--quiet","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
subprocess.run(["git","diff","--exit-code",results["upstream"],"HEAD","--","pkg/state","pkg/sentry","runsc"],check=True)
shutil.move(tmp/"nginx.tar",workspace/"test/rbe/graph_profile/nginx.tar")
container=["docker","exec","-e","GIT_CONFIG_COUNT=1","-e","GIT_CONFIG_KEY_0=safe.directory","-e","GIT_CONFIG_VALUE_0="+str(workspace),"overlay-guest"]
bep=tmp/"profile-bep.jsonl"
args=["bazel","--bazelrc="+str(tmp/"overlay.bazelrc"),"test","--config=x86_64","-c","opt","--jobs=12",
      "--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
      "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
      "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
      "--strategy=TestRunner=remote","--test_timeout=750","--remote_download_regex=.*(outputs[.]zip|test[.]log|test[.]xml)$","--nocache_test_results","--remote_download_outputs=toplevel","--zip_undeclared_test_outputs",
      "--test_output=errors","--build_event_json_file="+str(bep),target]
try:
    with (tmp/"profile.log").open("wb") as stream:
        p=subprocess.run(container+args,stdout=stream,stderr=subprocess.STDOUT)
    results["bazel_exit"]=p.returncode
    results["execution_finished"]=datetime.now(timezone.utc).isoformat()
    lines=(tmp/"profile.log").read_text(errors="replace").splitlines()
    results["invocations"]=sorted(set(re.findall(r'https://app.buildbuddy.io/invocation/[\da-f-]+',"\n".join(lines))))
    results["target_status"]=[e["testSummary"]["overallStatus"] for e in map(json.loads,bep.open()) if e["id"].get("testSummary",{}).get("label")==target]
    if p.returncode or results["target_status"]!=["PASSED"]:
        safe=[r[:550] for r in lines if re.search(r'ERROR:|FAILED|FAIL:|failed|error:|unexpected|fatal|timed out|no matching',r,re.I) and not re.search(r'api[-_ ]?key|secret|token|remote_header|bes_header|authorization:',r,re.I)]
        (out/"diagnostics.txt").write_text("\n".join(safe[-35:])+"\n")
    zpath=tmp/"profiles.zip"
    with zpath.open("wb") as f: subprocess.run(container+["cat","bazel-testlogs/test/rbe/graph_profile/nginx/test.outputs/outputs.zip"],stdout=f,check=True)
    with ZipFile(zpath) as z:
        for p in z.namelist():
            name=Path(p).name
            if re.fullmatch(r'(input|binaries|roundtrip)\.txt|(?:Decode|Encode)-(?:ordinary[1-3]|cpu|alloc)(?:\.bench|\.pprof|-reloads)',name):
                (out/name).write_bytes(z.read(p))
    for stem in results["expected"]:
        direction,role=stem.split("-")
        text=(out/(stem+".bench")).read_text()
        assert re.search(r'BenchmarkCheckpointFile/'+direction+r'Statefile(?:-\d+)?\s+[1-9]\d*\s+[\d.]+ ns/op',text) and 'B/op' in text and 'allocs/op' in text,text
        if direction=="Encode":
            assert re.search(r'^[1-9]\d* [a-f0-9]{64}$',(out/(stem+"-reloads")).read_text(),re.M)
        if role in ("cpu","alloc"):
            prof=out/(stem+".pprof")
            assert prof.stat().st_size>100
            for typ in (["cpu"] if role=="cpu" else ["alloc_space","alloc_objects"]):
                scope=("saveGraph" if direction=="Encode" else "loadGraph")
                for cumulative in (False,True):
                    cmd=["go","tool","pprof","-top","-nodecount=18","-nodefraction=0","-relative_percentages", "-sample_index="+typ,"-focus=gvisor.dev/gvisor/pkg/state\\."+scope]
                    if cumulative: cmd.append("-cum")
                    final=subprocess.check_output([*cmd,str(prof)],stderr=subprocess.STDOUT,text=True)
                    assert "Showing nodes accounting for" in final and "flat" in final, final[:400]
                    (out/(stem+"-"+typ+("-cumulative" if cumulative else "-flat")+".txt")).write_text(final)
        results["observed"].append(stem)
    assert results["bazel_exit"]==0 and results["target_status"]==["PASSED"]
except Exception as e:
    results["failures"].append(str(e)[:500])
finally:
    results["verified_at"]=datetime.now(timezone.utc).isoformat()
    emit()
assert not (results["missing"] or results["failures"]),results
