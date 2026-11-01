"""Compare the existing graph reader on one fixed checkpoint and the native guest."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from xml.etree import ElementTree

root=Path(os.environ["RUNNER_TEMP"])/"pair"
out=root/"records"
out.mkdir(parents=True, exist_ok=True)
phase=sys.argv[1]
sources={key:os.environ[key.upper()] for key in ("stock","fixed")}
gnu=["docker","exec","tcp-compiler"]
bz=gnu+["bazel","--bazelrc="+os.environ["RUNNER_TEMP"]+"/tcp.bazelrc"]
opts=["--config=x86_64","--spawn_strategy=remote,local","--noremote_upload_local_results","--strategy=GoStaticAnalysis=local","--strategy=GoStandardLibraryAnalysis=local","--remote_download_outputs=toplevel","--jobs=400"]
def record(name, data):
    (out/name).write_text(json.dumps(data,indent=2)+"\n")
def command(name, args, *, allow_failure=False, get_stdout=False):
    start=time.time_ns()
    stdout=root/(name+".stdout")
    log=root/(name+".log")
    with stdout.open("wb") as o, log.open("wb") as e:
        p=subprocess.run(args,stdout=o,stderr=e)
    text=log.read_text(errors="replace")
    end=time.time_ns()
    line=[x for x in text.splitlines() if re.search(r"^[^ :]+\.go:\d+:|^ERROR:|^FAILED:|Build did NOT complete successfully|Invocation ID:|loading.*failed",x)]
    record(name+"-command.json",{"exit":p.returncode,"start":start,"end":end,"diagnostics":[v[:360] for v in line[-10:] if not re.search(r"remote_header|api.key|authorization|cookie=",v,re.I)]})
    if p.returncode and not allow_failure:
        raise RuntimeError(f"{name} exited {p.returncode}; see its redacted command receipt")
    return stdout.read_text() if get_stdout else p.returncode
def select(arm):
    sha=sources[arm]
    subprocess.run(["git","checkout","-q","--detach",sha],check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==sha
    subprocess.run(["git","diff","--exit-code","--quiet"],check=True)
def output_file(name, target):
    found=command(name,bz+["cquery",*opts,"--output=files",target],get_stdout=True).splitlines()
    matches=[Path(x).absolute() for x in found if x and subprocess.run(gnu+["test","-f",x,"-a","-x",x]).returncode==0]
    if len(matches)!=1:raise ValueError((target,matches))
    return matches[0]
def suite(arm, *, race=False):
    name = arm + ("-race" if race else "")
    select(arm)
    owners=["//pkg/state:state_test", "//pkg/state/tests:tests_test"]
    args=["test",*opts,"--test_arg=-test.v","--test_sharding_strategy=disabled",
          "--nocache_test_results","--strategy=TestRunner=local"]
    if race:
        args.append("--config=race")
    code=command(name+"-state-suites",bz+args+owners,allow_failure=True)
    results={}
    for owner in owners:
        path="bazel-testlogs/"+owner[2:].replace(":","/")
        raw=subprocess.check_output(gnu+["cat",path+"/test.xml"])
        tests=list(ElementTree.fromstring(raw).iter("testcase"))
        failed=[t.get("name") for t in tests if any(t.find(k) is not None for k in ("error","failure"))]
        skipped=[t.get("name") for t in tests if t.find("skipped") is not None or t.get("status") in ("notrun","suppressed")]
        wanted=(["TestInlineLoadHook", "TestGraphCallbackOrder", "TestGraphDeferredObjectDecode", "TestGraphDecodeMapAndLoadValueDirectStructWithHooks", "TestGraphDecodeMapPopulatedBeforeCallbacks", "TestPrintGraphSliceNotation", "TestPrintGraphDeterministic", "TestPrintGraphUnexportedInlineMapAndTrailingData", "TestGraphRootMismatchAndPrintTime"]
                if owner == "//pkg/state:state_test" else [])
        names={t.get("name") for t in tests if t.get("name") not in skipped}
        missing=[n for n in wanted if n not in names]
        results[owner]={"count":len(tests),"failed":failed,"skipped":skipped,"missing":missing}
        record(name+"-suites.json",results)
    assert not code and all(v["count"] and not v["failed"] and not v["missing"] for v in results.values()),results
def file_digests(folder):
    names=("checkpoint.img","pages.img","pages_meta.img","spec.json")
    rows=subprocess.check_output(gnu+["sha256sum",*[str(folder/p) for p in names]],text=True).splitlines()
    sizes=subprocess.check_output(gnu+["stat","-c","%s",*[str(folder/p) for p in names]],text=True).splitlines()
    assert len(rows)==len(sizes)==len(names)
    return {p:{"bytes":int(size),"digest":row.split()[0]} for p,row,size in zip(names,rows,sizes)}
def live(i, arm):
    name=f"live-{i:02d}-{arm}"; select(arm)
    # Unmodified native fixture creates and verifies every file, including after restore.
    folder=root/("capture" if i==0 and arm=="stock" else name)
    folder.mkdir(exist_ok=True);folder.chmod(0o777)
    label="//runsc/container:container_test"
    args=["test",*opts,"--strategy=TestRunner=local","--test_filter=^TestCheckpointTmpfsTreeForProfile$","--test_env=WORKLOAD_CHECKPOINT_DIR="+str(folder),"--test_arg=-test.v","--test_sharding_strategy=disabled","--nocache_test_results","--test_timeout=600",label]
    code=command(name,bz+args,allow_failure=True)
    log=subprocess.check_output(gnu+["cat","bazel-testlogs/runsc/container/container_test/test.log"],text=True)
    lines=[x for x in log.splitlines() if "TMPFS-" in x or re.search(r"^--- (FAIL|PASS): TestCheckpointTmpfsTreeForProfile",x)]
    ns={k:int(v) for k,v in re.findall(r"TMPFS-NS ([a-z-]+) (\d+)",log)}
    data={"pair":i,"arm":arm,"source":sources[arm],"nanoseconds":ns,"evidence":lines,"exit":code}
    record(name+".json",data)
    assert not code and "TMPFS-VERIFIED 50000" in log and "TMPFS-GRAPH kernel=true" in log and "--- PASS: TestCheckpointTmpfsTreeForProfile" in log,lines
    assert set(ns)=={"new","checkpoint","restore","new-to-guest"} and all(f"TMPFS-CAP {s} 2147483648" in log for s in ("save","restore")),lines
    data["files"]=file_digests(folder);record(name+".json",data)
def prepare():
    original_bench=[]
    for arm in ("stock","fixed"):
        select(arm)
        original_bench.append(subprocess.check_output(["git","rev-parse","HEAD:test/benchmarks/checkpointfile/checkpointfile.go"],text=True).strip())
        label="//test/benchmarks/checkpointfile:checkpointfile_test"
        small = "//pkg/state/tests:tests_test"
        command(arm+"-build",bz+["build",*opts,label,small,"//runsc:runsc"])
        runsc = output_file(arm+"-runsc-path", "//runsc:runsc")
        subprocess.run(gnu+["cp",str(runsc),str(root/(arm+"-runsc"))],check=True)
        small_exe = output_file(arm+"-small-path", small)
        subprocess.run(gnu+["cp",str(small_exe),str(root/(arm+"-small"))],check=True)
        exe=output_file(arm+"-build-path",label)
        subprocess.run(gnu+["cp",str(exe),str(root/arm)],check=True)
    assert original_bench[0]==original_bench[1]=="bc63e141c336b10f6180a10229486f11f43d0fb7",original_bench
    hashes={arm:subprocess.check_output(gnu+["sha256sum",str(root/arm)],text=True).split()[0] for arm in ("stock","fixed")}
    assert len(set(hashes.values()))==2,hashes
    record("source.json",{"arms":sources,"benchmark_blob":original_bench[0],"benchmark_binaries":hashes,"product":os.environ['PRODUCT']})
def order(i):
    return ("stock","fixed") if i%2==0 else ("fixed","stock")
def stats():
    for arm in ["stock","fixed"]:
        lines=[]
        for row in json.loads((out/"paired-bench.json").read_text()):
            if row["arm"]==arm:lines.append(row["line"])
        for row in json.loads((out/"small-pairs.json").read_text()):
            if row["arm"] == arm:
                lines.append(row["line"])
        for row in json.loads((out/"boundary-rows.json").read_text()):
            if row["arm"] == arm:
                lines.append(row["lines"][0])
        for row in json.loads((out/"startup.json").read_text()):
            if row["arm"] == arm:
                lines.append(f"BenchmarkFreshVersion 1 {row['elapsed_ns']} ns/op {row['rss_kib'] * 1024} peak-rss-B")
        for i in range(8):
            ns=json.loads((out/f"live-{i:02d}-{arm}.json").read_text())["nanoseconds"]
            for key in ("restore","new-to-guest"):
                lines.append(f"BenchmarkNative/{key} 1 {ns[key]} ns/op")
        (out/(arm+".bench")).write_text("\n".join(lines)+"\n")
    benchstat("stock.bench", "fixed.bench", "benchstat.txt")
def benchstat(stock, fixed, report):
    tool="2b51abee8530385431b7225198fdd5cd65e78696"
    subprocess.run(["git","fetch","-q","--depth=1","origin",tool],check=True)
    subprocess.run(["git","checkout","-q","--detach",tool],check=True)
    label="@org_golang_x_perf//cmd/benchstat"
    command("benchstat-build",bz+["build",*opts,label])
    binary=output_file("benchstat-output",label)
    p=subprocess.run(gnu+[str(binary),str(out/stock),str(out/fixed)],stdout=subprocess.PIPE,text=True,check=True)
    (out/report).write_text(p.stdout)
if phase=="positive":
    suite("fixed")
elif phase=="race":
    suite("fixed", race=True)
elif phase=="analyzers":
    select("fixed")
    owners=["//pkg/state:state_nogo", "//pkg/state:state_test_nogo", "//pkg/state/tests:tests_nogo", "//tools/go_stateify:stateify_nogo"]
    results={}
    for arch in ("x86_64", "aarch64"):
        flags=["--config="+arch, *opts[1:]]
        code=command("fixed-analyzers-"+arch,bz+["test",*flags,"--strategy=TestRunner=local",*owners],allow_failure=True)
        results[arch]={"owners":owners,"exit":code}
        record("analyzers.json",results)
    assert all(row["exit"] == 0 for row in results.values())
elif phase=="prepare":prepare()
elif phase=="native":
    for i in range(8):
        for arm in order(i):
            live(i, arm)
elif phase in ("small","boundary","benchmark","capacity","startup"):
    subprocess.run(gnu+["python3",str(root/"pair-bench.py"),str(root),phase],check=True)
elif phase=="stats":stats()

elif phase=="small-stats":benchstat("stock-small.bench", "fixed-small.bench", "small-benchstat.txt")
