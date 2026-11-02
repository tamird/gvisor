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
    record(name+"-command.json",{"exit":p.returncode,"start":start,"end":end,"diagnostics":[re.sub(r"(?:https?|grpcs?|bytestream)://[^\s]+", "<url>", v)[:360] for v in line[-10:] if not re.search(r"remote_header|api.key|authorization|cookie=",v,re.I)]})
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
def suite(arm, *, race=False, focus=False):
    name = arm + ("-race" if race else "-order" if focus else "")
    select(arm)
    owners = ["//pkg/state:state_test"]
    if not focus:
        owners.append("//pkg/state/tests:tests_test")
    args = ["test", *opts, "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--test_sharding_strategy=disabled", "--nocache_test_results",
            "--strategy=TestRunner=local", "--test_timeout=240" if race else "--test_timeout=120"]
    if race:
        args.append("--config=race")
    if focus:
        args.append("--test_filter=^Test(GraphDeferredPasses|DeferredTraversalOrder)$")
    code = command(name + "-state-suites", bz + args + owners, allow_failure=True)
    results = {"source": sources[arm], "exit": code, "owners": {}}
    record(name + "-suites.json", results)
    required = {"TestDeferredTraversalOrder"}
    required.update(f"TestGraphDeferredPasses/n{n}/seeded_{v}" for n in (16, 64, 256, 1024) for v in ("true", "false"))
    if not focus:
        required.update({"TestGraphCallbackOrder", "TestGraphMapPayloadLength",
                         "TestGraphDeferredObjectDecode/unknown_deferred_type_error",
                         "TestGraphDeferredObjectDecode/deferred_trailing_data_error",
                         "TestGraphDecodeMapAndLoadValueDirectStructWithHooks",
                         "TestGraphDecodeMapPopulatedBeforeCallbacks", "TestPrintGraphSliceNotation",
                         "TestPrintGraphDeterministic", "TestPrintGraphUnexportedInlineMapAndTrailingData",
                         "TestGraphRootMismatchAndPrintTime"})
    for owner in owners:
        path = "bazel-testlogs/" + owner[2:].replace(":", "/")
        raw = subprocess.check_output(gnu + ["cat", path + "/test.xml"])
        (out / (name + "-" + path.rsplit("/", 1)[-1] + ".xml")).write_bytes(raw)
        cases = list(ElementTree.fromstring(raw).iter("testcase"))
        failed = [t.get("name") for t in cases if any(t.find(k) is not None for k in ("error", "failure"))]
        skipped = [t.get("name") for t in cases if t.find("skipped") is not None or t.get("status") in ("notrun", "suppressed") or t.get("result") == "suppressed"]
        names = {t.get("name") for t in cases} - set(skipped)
        wanted = required if owner == owners[0] else {"TestSharedSliceWindows/graph/middle/window_first=true"}
        missing = sorted(wanted - names)
        result = {"total": len(cases), "executed": len(cases) - len(skipped), "failed": failed, "skipped": skipped, "missing": missing,
                  "xml_sha256": hashlib.sha256(raw).hexdigest()}
        results["owners"][owner] = result
        record(name + "-suites.json", results)
        if focus:
            log = subprocess.check_output(gnu + ["cat", path + "/test.log"], text=True)
            observations = [line.split("SCAN-ORDER ", 1)[1] for line in log.splitlines() if "SCAN-ORDER " in line]
            record(name + ".json", observations)
    assert not code and all(v["executed"] > 0 and not v["failed"] and not v["missing"] for v in results["owners"].values()), results

def compare_order():
    suite("fixed", focus=True)
    # This is the original complete stock-order.json from the unchanged
    # baseline. It contains wire hashes, call and hook traces and first errors.
    expected = "7cca4247b7d0baa3637d499e4a3200b84867baa9f3efd3489adc6b4b87ed3cbc"
    actual = hashlib.sha256((out / "fixed-order.json").read_bytes()).hexdigest()
    record("baseline-order-source.json", {"run": 41375804192, "artifact": 13113190350,
           "archive": "3e005bc64e86720e2ae6a4a889c41cdcabcd76294ca8510e30d3a5f1b2628ade",
           "source": sources["stock"], "path": "stock-order.json", "expected": expected, "actual": actual})
    assert actual == expected
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
        chain = "//pkg/state:state_test"
        command(arm+"-build",bz+["build",*opts,label,small,chain])
        for role, target in (("", label), ("-small", small), ("-chain", chain)):
            exe = output_file(arm + role + "-build-path", target)
            subprocess.run(gnu + ["cp", str(exe), str(root / (arm + role))], check=True)
    assert original_bench == ["588cc13ed911b4a04f5d192032a5afa315be61a0"] * 2, original_bench
    hashes = {role:{arm:subprocess.check_output(gnu+["sha256sum",str(root/(arm+role))],text=True).split()[0]
                    for arm in ("stock","fixed")} for role in ("", "-small", "-chain")}
    assert all(len(set(group.values())) == 2 for group in hashes.values()), hashes
    record("source.json", {"arms": sources, "benchmark_blob": original_bench[0], "binaries": hashes, "product": os.environ["PRODUCT"]})
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
        for row in json.loads((out/"chain-bench.json").read_text()):
            if row["arm"] == arm:
                lines.extend(row["lines"])
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
elif phase=="order":compare_order()
elif phase=="prepare":prepare()
elif phase=="native":
    for i in range(8):
        for arm in order(i):
            live(i, arm)
elif phase in ("small","chain","benchmark"):
    subprocess.run(gnu+["python3",str(root/"pair-bench.py"),str(root),phase],check=True)
elif phase=="stats":stats()

elif phase=="corpus":
    # Instrument one additional untimed replay, after both measured binaries are immutable.
    select("fixed")
    target = "//test/benchmarks/checkpointfile:checkpointfile_test"
    source = Path("pkg/state/graph_decode_callbacks_unsafe.go")
    before = source.read_bytes()
    text = before.decode()
    edits = {
        "\tif len(deferred) != 0 {\n\t\tds.decodeDeferred(deferred)\n\t}":
            '\tindexed := 0\n\tif len(deferred) != 0 {\n\t\tindexed = ds.decodeDeferred(deferred)\n\t}\n'
            '\tfmt.Printf("DEFERRED-CORPUS objects=%d rows=%d indexed=%d\\n", len(ds.objs), len(deferred), indexed)',
        "decodeDeferred(rows []deferredObject) {": "decodeDeferred(rows []deferredObject) int {",
        "\tds.in.pos = end\n}": "\tds.in.pos = end\n\treturn len(remaining)\n}",
    }
    for original, observed in edits.items():
        assert text.count(original) == 1, original
        text = text.replace(original, observed)
    try:
        source.write_text(text)
        command("corpus-build", bz + ["build", *opts, target])
        binary = output_file("corpus-path", target)
        capture = root / "capture"
        hashes = file_digests(capture)
        lines = command("corpus-observer", gnu+["env","GO_TEST_WRAP=0","GOMAXPROCS=1",str(binary),"-checkpoint-dir="+str(capture),"-test.cpu=1","-test.run=^$","-test.bench=^BenchmarkCheckpointFile$/^DecodeStatefile$","-test.benchtime=1x"], get_stdout=True)
        matches = re.findall(r"DEFERRED-CORPUS objects=(\d+) rows=(\d+) indexed=(\d+)", lines)
        record("corpus.json", {"instrumented_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "frozen": hashes,
                               "counts": [dict(zip(("objects","rows","indexed"),map(int,row))) for row in matches]})
        assert matches and max(int(row[0]) for row in matches) > 50000 and re.search(r"^PASS$",lines,re.M) and file_digests(capture)==hashes
    finally:
        source.write_bytes(before)
