"""Record live seqpacket shutdown tests and the installed netgofer filter decisions."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

out = Path(os.environ["RUNNER_TEMP"]) / "netgofer-original"
bazel = [str(out / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
opts = ["--config=x86_64", "-c", "opt", "--jobs=4", "--local_resources=memory=8192", "--strategy=GoStdlib=processwrapper-sandbox",
        "--strategy=GoStdlibList=processwrapper-sandbox", "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
        "--remote_cache=", "--remote_executor=", "--bes_backend="]
targets=["//runsc/gofer/net:netgofer_test"]
test_names=re.findall(r"^func (Test\w+)\(",Path("runsc/gofer/net/netgofer_test.go").read_text(),re.M)
assert len(test_names)==6 and "TestRunProxyModeWithSeccomp" in test_names
result = {"source":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(), "owners":{}, "cases":[], "controls":[], "pairs":[]}
def save():
    (out / "result.json").write_text(json.dumps(result, indent=2)+"\n")
assert result["source"] == "37d0d0fedafcaa8dcd4901d47d9bde87cbca85d2"
save()
with (out / "test.console").open("w") as f:
    test = subprocess.run([*bazel, "test", *opts, "--test_env=GOMAXPROCS=2", "--build_event_publish_all_actions",
        "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
        "--nocache_test_results","--flaky_test_attempts=1","--test_timeout=360","--test_arg=-test.timeout=310s","--test_arg=-test.count=25",
        "--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1",
        "--keep_going","--build_event_json_file="+str(out/"events.jsonl"),*targets], stdout=f,stderr=subprocess.STDOUT)
result["exit"] = test.returncode
events=out/"events.jsonl"
if events.is_file():
    for line in events.open():
        event=json.loads(line)
        if "testResult" not in event: continue
        label=event["id"]["testResult"]["label"]
        assert label in targets
        item=event["testResult"]
        info=item.get("executionInfo",{})
        result["owners"][label]={"status":item["status"],"strategy":info.get("strategy"),"cached":bool(item.get("cachedLocally") or info.get("cachedRemotely"))}
for label in targets:
    pkg, name = label[2:].split(":")
    log=Path("bazel-testlogs")/pkg/name/"test.log"
    if not log.is_file() or label not in result["owners"]: continue
    data=log.read_bytes()
    result["owners"][label]["log_sha256"]=hashlib.sha256(data).hexdigest()
    for line in data.decode(errors="replace").splitlines():
        if m:=re.search(r"--- (PASS|FAIL): (Test[A-Za-z0-9_/]+) ",line):
            result["cases"].append(m.groups())
        if re.search(r"\.go:\d+:|^panic: test timed out|^--- FAIL:",line) and not re.search(r"QUEUE |PENDING |CONFINED |DRAIN |(?i:token|secret|authorization)",line):
            if len(result.setdefault("native_notes",[]))<18: result["native_notes"].append(line.strip()[:300])
console=(out/"test.console").read_text(errors="replace")
result["build_errors"]=[line[:300] for line in console.splitlines() if re.search(r"^ERROR:|\.go:\d+:|Undefined reference|no useable spawn strategy",line) and not re.search(r"(?i)token|secret|authorization|--remote_header",line)][:22]
save()
counts={n:sum((s=="PASS" and name==n) for s,name in result["cases"]) for n in test_names}
result["positive_counts"]=counts
result["owner_source_blob"]=subprocess.check_output(["git","hash-object","runsc/gofer/net/netgofer_test.go"],text=True).strip()
result["verified"]=(result["exit"]==0 and result["owner_source_blob"]=="1b0f0fdacf9198ef8863117080b59801519ddc64"
                     and all(n==25 for n in counts.values()) and result["owners"].keys()==set(targets)
                     and all(v["status"]=="PASSED" and v["strategy"]=="linux-sandbox" and not v["cached"] for v in result["owners"].values()))
save()
raise SystemExit(not result["verified"])
