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
sources={"stock":os.environ["SOURCE"]}
gnu=["docker","exec","tcp-compiler"]
bz=gnu+["bazel","--bazelrc="+os.environ["RUNNER_TEMP"]+"/tcp.bazelrc"]
opts=["--config=x86_64","--spawn_strategy=remote,local","--noremote_upload_local_results","--strategy=GoStaticAnalysis=local","--strategy=GoStandardLibraryAnalysis=local","--remote_download_outputs=toplevel","--jobs=24"]
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
def suite(arm):
    select(arm)
    owners=["//pkg/state:state_test", "//pkg/state/tests:tests_test"]
    args=["test",*opts,"--test_arg=-test.v","--test_sharding_strategy=disabled",
          "--nocache_test_results","--strategy=TestRunner=local"]
    code=command(arm+"-state-suites",bz+args+owners,allow_failure=True)
    results={}
    for owner in owners:
        path="bazel-testlogs/"+owner[2:].replace(":","/")
        raw=subprocess.check_output(gnu+["cat",path+"/test.xml"])
        tests=list(ElementTree.fromstring(raw).iter("testcase"))
        failed=[t.get("name") for t in tests if any(t.find(k) is not None for k in ("error","failure"))]
        skipped=[t.get("name") for t in tests if t.find("skipped") is not None or t.get("status") in ("notrun","suppressed")]
        wanted=(["TestGraphDeferredObjectDecode", "TestGraphDecodeMapAndLoadValueDirectStructWithHooks", "TestGraphDecodeMapPopulatedBeforeCallbacks", "TestPrintGraphSliceNotation", "TestPrintGraphDeterministic", "TestPrintGraphUnexportedInlineMapAndTrailingData", "TestGraphRootMismatchAndPrintTime"]
                if owner == "//pkg/state:state_test" else [])
        names={t.get("name") for t in tests if t.get("name") not in skipped}
        missing=[n for n in wanted if n not in names]
        results[owner]={"count":len(tests),"failed":failed,"skipped":skipped,"missing":missing}
        record(arm+"-suites.json",results)
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
    select("stock")
    label="//test/benchmarks/checkpointfile:checkpointfile_test"
    command("census-build",bz+["build",*opts,label])
    binary=output_file("census-binary",label)
    subprocess.run(gnu+["cp",str(binary),str(root/"census")],check=True)
    digest=subprocess.check_output(gnu+["sha256sum",str(root/"census")],text=True).split()[0]
    record("binary.json",{"source":sources["stock"],"target":label,"sha256":digest})

def census():
    frozen=file_digests(root/"capture")
    args=gnu+["env","STATE_CALLBACK_CENSUS=1","GO_TEST_WRAP=0",str(root/"census"),"-checkpoint-dir="+str(root/"capture"),"-test.cpu=1","-test.run=^$","-test.bench=^BenchmarkCheckpointFile$/^DecodeStatefile$","-test.benchtime=1x"]
    code=command("census",args,allow_failure=True)
    finished=time.time_ns()
    text=(root/"census.stdout").read_text()+(root/"census.log").read_text()
    graphs=[json.loads(line.removeprefix("STATE-CALLBACK-CENSUS ")) for line in text.splitlines() if line.startswith("STATE-CALLBACK-CENSUS ")]
    data={"source":sources["stock"],"baseline":os.environ["BASE"],"exit":code,"graphs":graphs,"checkpoint":frozen,"executionFinishedNs":finished,"interpretation":"Registration census only. Timing and allocation data are instrumented and are not performance evidence."}
    record("census.json",data)
    assert code==0 and re.search(r"^PASS$",text,re.M) and graphs,data
    assert any(g["Records"]>100_000 for g in graphs),graphs
    for graph in graphs:
        assert sum(row["Count"] for row in (graph["Callbacks"] or []))==graph["Records"],graph
        expected=sum(row["Count"] for row in (graph["Callbacks"] or []) if row["Origin"].endswith((".decodeMap",".decodeElems")) and row["Kind"]=="dependency")
        observed=sum(row["Count"]*row["Dependencies"] for row in (graph["Batches"] or []))
        assert expected==observed,(expected,observed)
    assert frozen==file_digests(root/"capture")
    data["status"]="PASS";data["verifiedAtNs"]=time.time_ns()
    record("census.json",data)

if phase=="positive":suite("stock")
elif phase=="prepare":prepare()
elif phase=="native":live(0,"stock")
elif phase=="census":census()
else:raise ValueError(phase)
