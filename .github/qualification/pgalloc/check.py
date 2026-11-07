"""Check the allocator commitments and measure the original recycled-page workload."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET


BASE = "70008149cff7b8d3b858877ae9c359b9046d19c5"
FIX = "61470ea4162137e6871edd94e369f1c03592c163"
WORK = Path(os.environ["RUNNER_TEMP"], "pgalloc-review")
OUT = WORK / "results"
PKG = Path("pkg/sentry/pgalloc")
BUILD = PKG / "BUILD"
TEST = PKG / "pgalloc_test.go"
BENCH = PKG / "pgalloc_recycled_benchmark_test.go"
TARGET = "//pkg/sentry/pgalloc:pgalloc_test"
SAFECOPY = "//pkg/safecopy:safecopy_test"
TMPFS = "//pkg/sentry/fsimpl/tmpfs:tmpfs_test"
FILE_HASH = "46a42015883f84fac6ff64ef1a1735211251bf5bcc965e709e7a92fcbe8ee4f9"
ADVISED = "TestAllocateAndCommit/small_advised_allocation"
DISK = "TestAllocateRecycledDiskBacked"
DISK_WITNESSES = {f"{DISK}/{mode}/{page}" for mode in ("commit","populate")
                  for page in ("all-writable","skip-zero-page")}
ALLOC_WITNESSES = {"TestAllocateAndCommit/"+name for name in (
    "small_advised_allocation","recycled_allocation","recycled_fault",
    "recycled_fault_after_first_page","fault_across_hugepage_boundary",
    "huge_advised_fault","commit_after_backing_fault")}
BMARK = re.compile(r"^(BenchmarkAllocateRecycled/(?:4K|64K|2M)/(?:hot|cold))(?:-1)?\s+\d+\s+[0-9.]+ ns/op\s+.*$")
CPU = min(os.sched_getaffinity(0))


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save(report):
    (OUT / "results.json").write_text(json.dumps(report, sort_keys=True, indent=1)+"\n")


def setup(source, *, new_tests=False, benchmark=False):
    git("restore", "--worktree", "--source=HEAD", "--", str(BUILD), str(TEST))
    BENCH.unlink(missing_ok=True)
    git("checkout", "-q", "--detach", source)
    assert git("rev-parse", "HEAD") == source
    if new_tests:
        assert source == BASE
        git("restore", "--worktree", "--source="+FIX, "--", str(TEST))
        # Use the revised test deps while preserving the base production BUILD.
        text = BUILD.read_text()
        start = 'go_test(\n    name = "pgalloc_test",'
        old_before, old_test = text.split(start)
        fixed = git("show", FIX+":"+str(BUILD)).split(start)[1].split("\n)",1)[0]
        BUILD.write_text(old_before+start+fixed+"\n)"+old_test.split("\n)",1)[1])
    if benchmark:
        shutil.copyfile(WORK / "recycled_benchmark.go", BENCH)
        assert hashlib.sha256(BENCH.read_bytes()).hexdigest() == FILE_HASH
        text = BUILD.read_text()
        marker = '        "pgalloc_test.go",'
        assert text.count(marker) == 1
        BUILD.write_text(text.replace(marker, '        "'+BENCH.name+'",\n'+marker))
    modified=set(git("diff", "--name-only", "HEAD").splitlines())
    assert modified == ({str(BUILD),str(TEST)} if new_tests else {str(BUILD)} if benchmark else set())


def run(report, name, target, flags, *, witnesses=(), fails=None, sample=None):
    events = WORK / "events.json"
    events.unlink(missing_ok=True)
    console = WORK / (name+".console")
    command = [str(WORK/"bazel"), "--nosystem_rc", "--nohome_rc", "test", "-c", "opt",
               "--config=x86_64", "--jobs=4", "--remote_executor=", "--remote_cache=", "--bes_backend=",
               "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
               "--test_sharding_strategy=disabled", "--test_timeout=360",
               "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=GOMAXPROCS=1",
               "--run_under=/usr/bin/taskset -c "+str(CPU),
               "--build_event_json_file="+str(events), *["--test_arg="+s for s in flags], target]
    row={"name":name, "source":git("rev-parse","HEAD"), "label":target}
    report["runs"].append(row)
    save(report)
    with console.open("w") as stream:
        row["exit"]=subprocess.run(command,stdout=stream,stderr=stream).returncode
    results=[]
    if events.is_file():
        with events.open() as stream:
            for line in stream:
                event=json.loads(line)
                if event["id"].get("testResult",{}).get("label")==target and "testResult" in event:
                    results.append(event["testResult"])
    if not results:
        row["diagnostic"]=[line[:400] for line in console.read_text(errors="replace").splitlines()
                           if "ERROR:" in line or re.search(r"[.]go:\d+:\d+:",line)][-10:]
        save(report)
        raise RuntimeError(name+" did not execute")
    result,=results
    row.update(status=result["status"],cached=bool(result.get("cachedLocally") or
               result.get("executionInfo",{}).get("cachedRemotely")),
               strategy=result.get("executionInfo",{}).get("strategy"))
    files={}
    for item in result.get("testActionOutput",[]):
        if item["name"] in ("test.log","test.xml"):
            uri=urlparse(item["uri"])
            assert uri.scheme=="file"
            data=Path(unquote(uri.path)).read_bytes()
            assert len(data)<8*1024*1024
            files[item["name"]]=data
            row[item["name"]+"_sha256"]=hashlib.sha256(data).hexdigest()
    save(report)
    assert not row["cached"] and row["strategy"] in ("processwrapper-sandbox","linux-sandbox","local")
    log=files["test.log"].decode(errors="replace")
    if sample:
        lines=[line.strip() for line in log.splitlines() if BMARK.fullmatch(line.strip())]
        row["benchmark_rows"]=len(lines)
        save(report)
        expected={"BenchmarkAllocateRecycled/"+size+"/"+temp for size in ("4K","64K","2M") for temp in ("hot","cold")}
        assert len(lines)==6 and {BMARK.fullmatch(line)[1] for line in lines}==expected
        if sample!="warmup":
            with (OUT/sample).open("a") as stream:stream.write("\n".join(lines)+"\n")
    else:
        cases={}
        for case in ET.fromstring(files["test.xml"]).iter("testcase"):
            status="passed"
            if any(case.find(tag) is not None for tag in ("failure","error")):status="failed"
            elif case.find("skipped") is not None or case.get("status")=="notrun":status="skipped"
            cases[case.get("name")]=status
        row["counts"]=dict(Counter(cases.values()))
        row["named"]={key:cases.get(key) for key in set(witnesses)|set(fails or {})}
        if fails:
            row["expected_error"]={key:marker in log for key,marker in fails.items()}
        save(report)
        assert all(cases.get(key)=="passed" for key in witnesses)
        if fails:
            assert row["status"]=="FAILED" and row["exit"]!=0
            assert all(cases.get(key)=="failed" and marker in log for key,marker in fails.items())
            print(name,"EXPECTED FAILURE",flush=True)
            return
        assert "failed" not in cases.values()
    assert row["exit"]==0 and row["status"]=="PASSED"
    print(name,"PASSED",flush=True)


def main():
    info=Path("/proc/cpuinfo").read_text().splitlines()
    report={"base":BASE,"fixed":FIX,"arch":platform.machine(),"cpu":CPU,
            "cpu_model":next(x.split(":",1)[1].strip() for x in info if x.startswith("model name")),
            "erms":any(" erms " in x+" " for x in info if x.startswith("flags")),
            "benchmark_sha256":FILE_HASH,"pairs":16,"runs":[],"verified":False}
    save(report)
    assert report["arch"]=="x86_64" and git("rev-parse",FIX+"^")==BASE
    # Master controls exercise only healthy backing and protected disk pages.
    setup(BASE,new_tests=True)
    run(report,"base-advised",TARGET,["-test.v","-test.run=^TestAllocateAndCommit$/^small_advised_allocation$"],
        fails={ADVISED:"committed bytes before ReaderFunc = 0"})
    run(report,"base-recycled-disk",TARGET,["-test.v","-test.run=^TestAllocateRecycledDiskBacked$"],
        witnesses=DISK_WITNESSES-{DISK+"/commit/all-writable"},
        fails={DISK+"/commit/all-writable":"want empty range and EBADF"})
    setup(FIX)
    run(report,"fixed-pgalloc",TARGET,["-test.v"],witnesses=ALLOC_WITNESSES|DISK_WITNESSES|{
        "TestZeroDiskBackedSkipsZeroUnwritablePage","TestZeroDiskBackedPanicsOnNonzeroUnwritablePage"})
    run(report,"fixed-safecopy",SAFECOPY,["-test.v"],witnesses={
        "TestZeroOutSuccess","TestZeroOutSegvError","TestZeroOutBusError"})
    run(report,"fixed-tmpfs",TMPFS,["-test.v","-test.run=^(TestSimpleWriteRead|TestTruncate)$"],
        witnesses={"TestSimpleWriteRead","TestTruncate"})
    for index in range(17):
        for source in ((BASE,FIX) if index%2 else (FIX,BASE)):
            setup(source,benchmark=True)
            sample="warmup" if index==0 else source[:9]
            run(report,f"pair-{index:02}-{source[:9]}",TARGET,["-test.run=^$",
                "-test.bench=^BenchmarkAllocateRecycled$","-test.benchmem","-test.cpu=1",
                "-test.count=1","-test.benchtime="+("100ms" if index==0 else "1s")],sample=sample)
    report["verified"]=True
    save(report)


if __name__=="__main__":
    main()
