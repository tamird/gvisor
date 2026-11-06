"""Verify native SEQPACKET shutdown controls and paired TCP proxy measurements."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

out = Path(os.environ["RUNNER_TEMP"]) / "netgofer-timestamp"
bazel = [str(out / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
opts = ["--config=x86_64", "-c", "opt", "--jobs=4", "--local_resources=memory=8192", "--strategy=GoStdlib=processwrapper-sandbox",
        "--strategy=GoStdlibList=processwrapper-sandbox", "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
        "--remote_cache=", "--remote_executor=", "--bes_backend="]
targets = ["//pkg/rawfile:timestamp_drain_test", "//pkg/tcpip/link/fdbased:fdbased_test", "//runsc/gofer/net:netgofer_test"]
test_names = ["TestFinalFrameArrivesAfterEAGAIN", "TestTimestampQueuedSeqpacketRecords", "TestTimestampProxyTransport", "TestRunProxyModeWithSeccomp"]
result = {"source":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(), "owners":{}, "cases":[], "controls":[], "pairs":[]}
def save():
    (out / "result.json").write_text(json.dumps(result, indent=2)+"\n")
assert result["source"] == "5077c89fd0be712d40890b77cc9fccd20711a3e1"
save()
with (out / "test.console").open("w") as f:
    test = subprocess.run([*bazel, "test", *opts, "--test_env=GOMAXPROCS=2", "--build_event_publish_all_actions",
        "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
        "--nocache_test_results","--flaky_test_attempts=1","--test_timeout=260","--test_arg=-test.timeout=210s",
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
        if m:=re.search(r"\b(QUEUED setup=(?:before|after) empties=\d+ delivered=\d+ eof=\d+|DRAIN arm=(?:stock|timestamp) revents=\d+ received=\d+ len=\d+ errno=\d+)$",line):
            result["controls"].append(m[1])
        if m:=re.search(r"MEASURE workload=(RPC64|Chunk32K) pair=(\d+) position=(0|1) timestamp=(0|1) n=(\d+) bytes=(\d+) elapsed_ns=(\d+)$",line):
            result["pairs"].append([m[1],*[int(v) for v in m.groups()[1:]]])
        if re.search(r"\.go:\d+:|^panic: test timed out|^--- FAIL:",line) and not re.search(r"MEASURE |QUEUED |DRAIN |(?i:token|secret|authorization)",line):
            if len(result.setdefault("native_notes",[]))<18: result["native_notes"].append(line.strip()[:300])
console=(out/"test.console").read_text(errors="replace")
result["build_errors"]=[line[:300] for line in console.splitlines() if re.search(r"^ERROR:|\.go:\d+:|Undefined reference|no useable spawn strategy",line) and not re.search(r"(?i)token|secret|authorization|--remote_header",line)][:22]
save()
rows=result["pairs"]
assert sorted((w,i,pos,on,n,b) for w,i,pos,on,n,b,dt in rows if dt>0)==sorted(
    (w,i,pos,(i+pos)%2,n,b) for w,n,b in (("RPC64",512,64),("Chunk32K",128,32768)) for i in range(24) for pos in range(2))
for setting,arm in enumerate(["off","on"]):
    with (out/(arm+".bench")).open("w") as f:
        for w,i,pos,on,n,b,dt in rows:
            if on==setting:
                print(f"BenchmarkNetgofer/{w} {n} {dt/n:.3f} ns/op",file=f)
donor="2b51abee8530385431b7225198fdd5cd65e78696"
subprocess.run(["git","fetch","--no-tags","--depth=1","https://github.com/tamird/gvisor.git",donor],check=True)
subprocess.run(["git","switch","--quiet","--detach",donor],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==donor
with (out/"benchstat.txt").open("w") as stats, (out/"analysis.console").open("w") as build:
    rc=subprocess.run([*bazel,"run",*opts,"--lockfile_mode=off","@org_golang_x_perf//cmd/benchstat","--",
                    "off="+str(out/"off.bench"),"on="+str(out/"on.bench")],stdout=stats,stderr=build).returncode
result["benchstat_exit"]=rc
if rc:
    result["benchstat_errors"]=[s[:300] for s in (out/"analysis.console").read_text(errors="replace").splitlines() if s.startswith("ERROR:")][:5]
save()
assert not rc
assert test.returncode==0 and len(result["controls"])==4
assert all(x["status"]=="PASSED" and x["strategy"]=="linux-sandbox" and not x["cached"] for x in result["owners"].values())
assert all(["PASS",n] in [list(e) for e in result["cases"]] for n in test_names)
