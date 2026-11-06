"""Record live seqpacket shutdown tests and the installed netgofer filter decisions."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

out = Path(os.environ["RUNNER_TEMP"]) / "netgofer-fionread"
bazel = [str(out / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
opts = ["--config=x86_64", "-c", "opt", "--jobs=4", "--local_resources=memory=8192", "--strategy=GoStdlib=processwrapper-sandbox",
        "--strategy=GoStdlibList=processwrapper-sandbox", "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
        "--remote_cache=", "--remote_executor=", "--bes_backend="]
targets = ["//pkg/rawfile:timestamp_drain_test", "//pkg/tcpip/link/fdbased:fdbased_test", "//runsc/gofer/net:netgofer_test","//runsc/gofer/net/filter/config:config_test"]
test_names = ["TestFinalFrameArrivesAfterEAGAIN", "TestSeqpacketQueueDrain", "TestSeqpacketPendingSocketError",
              "TestFIONREADOnOnlyNetgoferFD", "TestRunProxyModeWithSeccomp", "TestRunProxyModeEndToEnd",
              "TestOptionsConfigKey", "TestPrecompiledPrograms", "TestPrecompiledProgramsDeterministic"]
result = {"source":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(), "owners":{}, "cases":[], "controls":[], "pairs":[]}
def save():
    (out / "result.json").write_text(json.dumps(result, indent=2)+"\n")
assert result["source"] == "0d511aa607007f3800bbd423f7c13dd46e4940fd"
save()
with (out / "test.console").open("w") as f:
    test = subprocess.run([*bazel, "test", *opts, "--test_env=GOMAXPROCS=2", "--build_event_publish_all_actions",
        "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
        "--nocache_test_results","--flaky_test_attempts=1","--test_timeout=260","--test_arg=-test.timeout=210s","--test_arg=-test.count=3",
        "--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.run=^("+"|".join(test_names)+")$",
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
        if m:=re.search(r"\b(QUEUE case=(?:empty|behind_32_empty|ordinary) empty=\d+ delivered=\d+ ended=true|DRAIN arm=(?:stock|queue) revents=\d+ received=\d+ len=\d+ errno=\d+|CONFINED mode=(?:allowed|wrong-fd|wrong-command) allowed=(?:true|false)|PENDING revents=\d+ errno=\d+)$",line):
            result["controls"].append(m[1])
        if re.search(r"\.go:\d+:|^panic: test timed out|^--- FAIL:",line) and not re.search(r"QUEUE |PENDING |CONFINED |DRAIN |(?i:token|secret|authorization)",line):
            if len(result.setdefault("native_notes",[]))<18: result["native_notes"].append(line.strip()[:300])
console=(out/"test.console").read_text(errors="replace")
result["build_errors"]=[line[:300] for line in console.splitlines() if re.search(r"^ERROR:|\.go:\d+:|Undefined reference|no useable spawn strategy",line) and not re.search(r"(?i)token|secret|authorization|--remote_header",line)][:22]
save()
counts={n:sum((s=="PASS" and name==n) for s,name in result["cases"]) for n in test_names}
result["positive_counts"]=counts
check=lambda pattern: sum(bool(re.fullmatch(pattern,s)) for s in result["controls"])==3
patterns=[r"CONFINED mode=allowed allowed=true",r"CONFINED mode=wrong-fd allowed=false",r"CONFINED mode=wrong-command allowed=false",
          r"QUEUE case=empty empty=32 delivered=0 ended=true",r"QUEUE case=behind_32_empty empty=32 delivered=1 ended=true",
          r"QUEUE case=ordinary empty=0 delivered=1 ended=true",r"PENDING revents=\d+ errno=104",
          r"DRAIN arm=stock revents=\d+ received=0 len=0 errno=104",r"DRAIN arm=queue revents=\d+ received=1 len=15 errno=0"]
result["verified"]=(result["exit"]==0 and all(n==3 for n in counts.values()) and all(check(p) for p in patterns)
                     and result["owners"].keys()==set(targets) and all(v["status"]=="PASSED" and v["strategy"]=="linux-sandbox" and not v["cached"] for v in result["owners"].values()))
save()
raise SystemExit(not result["verified"])
