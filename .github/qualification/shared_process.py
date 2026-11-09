"""Qualify the exact gVisor test executables through Bazel."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

work = Path(__file__).resolve().parent
source = "122b98abdf94f0000340b489c1da77505051de0d"
proc = "//pkg/shim/v1/proc:proc_test"
suites = {
    "//runsc/specutils:specutils_test": "IsProcessRunning WaitForReadyHappy WaitForReadyFail WaitForReadyNotRunning WaitForReadyTimeout ProcessIdentity",
    "//runsc/cmd:cmd_test": "DeleteBootingSandbox KillBootingSandbox StateBootingSandbox ListUnresponsiveSandbox ReadOnlyContainerIsNotWritable DeleteReusedPID",
    "//pkg/shim/v1:shim_test": "ResolveGrouping ManagerInfo ManagerStopBoundsWedgedRuntime ManagerStopPreservesCallerCancellation ManagerStopBoundsBlockedUnmountAndReportsFailure ManagerStopCleansRootfsAfterDeleteFailure",
    proc: "KillAllWedgedRunscReturnsAndSparesSandbox StatsWedgedRunscTimesOut StatusWedgedRunscTimesOut KillWedgedRunscTimesOut RuntimeLockCancellationHandoff RuntimeCallsAreBounded CreateWedgedRunscIsBounded CreatePinsOnlyMatchingSandbox PinnedSandboxRemainsOwnedAcrossExec FuseStatusRetryPreservesOutcome ExecRuntimeCallsAreBounded ExecCanceledCallerDoesNotWaitBehindLock ExecDeleteBoundsIOWaitAndCanRetry DeleteClosesPidfdBeforeFailedUnmount InitDeleteBoundsIOWaitAndCanRetry InitCallsObserveCancellationWhileLocked RuntimeDeadlineIncludesLockWait ExplicitSandboxKillAfterFuseWatchdog SandboxExitClosesDeadPidfd InitExitRetainsLiveSandboxForExplicitDelete OnlyExplicitSandboxTeardownEscalates LongRuntimeOperationsAreBounded DeleteBoundsBlockedUnmountAndReusesWorker TeardownDoesNotInterruptCheckpoint DeleteClosesBlockedOutputPipe FailedDeleteRetainsOnlyLivePidfd DeleteTimeoutAfterSuccessfulKill",
    "//pkg/sentry/platform/systrap:systrap_test": "ThreadWaitStoppedWhenSignaled SleepOnStateStuckContext SleepOnStateStuckContextWhenStubIsGone SleepOnStateRepeatedInterruptsStillStuck SleepOnStateHostStall",
}
bazel = [str(work/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g"]
flags = ["-c","opt","--jobs=4","--lockfile_mode=off","--remote_cache=","--remote_executor=","--bes_backend=",
         "--spawn_strategy=linux-sandbox","--strategy=GoStdlib=processwrapper-sandbox","--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox"]
report = {"source":source,"carrier":os.environ["GITHUB_SHA"],"platform":os.uname().machine,"cells":{}}
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == source
assert report["platform"] == "x86_64"


def record():
    (work/"results.json").write_text(json.dumps(report,indent=2,sort_keys=True))


def test(name, targets, broken=False):
    bep = work/(name+".bep")
    cell = report["cells"][name] = {"owners":{},"verified":False}
    names = sorted({n for v in targets.values() for n in v.split()})
    cell["exit"] = subprocess.run([*bazel,"test",*flags,"--config=x86_64","--strategy=TestRunner=local",
        "--test_sharding_strategy=disabled","--nocache_test_results","--flaky_test_attempts=1","--keep_going","--test_output=errors",
        "--test_arg=-test.v","--test_arg=-test.run=^Test("+"|".join(names)+")$","--build_event_json_file="+str(bep),*targets]).returncode
    for line in bep.open():
        event = json.loads(line)
        if "testResult" not in event:
            continue
        owner = event["id"]["testResult"]["label"]
        data = event["testResult"]
        stem = name+"-"+owner.split(":")[-1]+"-"
        for f in data.get("testActionOutput",[]):
            if f["name"] in ("test.log","test.xml"):
                uri = urlsplit(f["uri"])
                assert uri.scheme == "file"
                shutil.copyfile(unquote(uri.path),work/(stem+f["name"]))
        rows = {c.get("name"):("skip" if c.find("skipped") is not None else "fail" if
            c.find("failure") is not None or c.find("error") is not None else "pass") for c in ET.parse(work/(stem+"test.xml")).findall(".//testcase")}
        cell["owners"][owner]={"cases":rows,"status":data["status"],"strategy":data.get("executionInfo",{}).get("strategy")}
    record()
    assert cell["exit"] == (3 if broken else 0) and cell["owners"].keys() == targets.keys(),cell
    for owner, names in targets.items():
        result=cell["owners"][owner]
        assert all("Test"+n in result["cases"] for n in names.split()),(owner,result)
        if broken:
            assert result["cases"]["TestStatusWedgedRunscTimesOut"]=="fail" and "was never killed" in (work/(name+"-proc_test-test.log")).read_text()
        else:
            assert result["status"]=="PASSED" and "fail" not in result["cases"].values()
    cell["verified"]=True
    record()


probe = r"""
// This fixture is present only on the qualification worker.
func TestQualificationSharedContextCleanup(t *testing.T) {
	var pid int
	t.Run("Live", func(t *testing.T) {
		pid = int(newTestSharedContext(t).subprocess.syscallThread.thread.tid)
		if err := unix.Kill(pid, 0); err != nil {
			t.Fatalf("child was not live before cleanup: %v", err)
		}
	})
	if err := unix.Kill(pid, 0); !errors.Is(err, unix.ESRCH) {
		t.Fatalf("child still exists after subtest cleanup: %v", err)
	}
	var status unix.WaitStatus
	if _, err := unix.Wait4(pid, &status, unix.WNOHANG, nil); !errors.Is(err, unix.ECHILD) {
		t.Fatalf("child was not reaped by subtest cleanup: %v", err)
	}
}

// Completes the eager reaper first to show a permitted ordering even if the
// normal, unmodified test does not happen to see it on this worker.
func TestQualificationEagerReaper(t *testing.T) {
	proc, exited := testprocess.Pause.Start(t)
	child := newChildSharedContext(proc.Pid).subprocess.syscallThread.thread
	child.kill()
	<-exited
	if _, err := child.wait(stopped); !errors.Is(err, unix.ECHILD) {
		t.Fatalf("wait4 after Cmd.Wait returned %v", err)
	}
}
"""


def compare(name, revision, fixed):
    import re
    subprocess.run(["git", "switch", "--quiet", "--detach", revision], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == revision
    target = "//pkg/sentry/platform/systrap:systrap_test"
    file = Path("pkg/sentry/platform/systrap/shared_context_test.go")
    original = file.read_bytes()
    file.write_bytes(original + probe.encode())
    runs = 128
    names = ("ThreadWaitStoppedWhenSignaled", "QualificationSharedContextCleanup", "QualificationEagerReaper")
    args = [*bazel, "test", *flags, "--config=x86_64", "--strategy=TestRunner=local",
            "--test_sharding_strategy=disabled", "--nocache_test_results", "--flaky_test_attempts=1",
            "--test_arg=-test.v", "--test_arg=-test.count=64", "--test_arg=-test.cpu=1,4",
            "--test_arg=-test.run=^Test(" + "|".join(names) + ")$",
            "--build_event_json_file=" + str(work/(name+".bep")), target]
    try:
        code = subprocess.run(args).returncode
    finally:
        file.write_bytes(original)
    events = (json.loads(line) for line in (work/(name+".bep")).read_text().splitlines())
    result, = (e["testResult"] for e in events if e.get("id",{}).get("testResult",{}).get("label")==target and "testResult" in e)
    log = next(item for item in result["testActionOutput"] if item["name"]=="test.log")
    path = Path(unquote(urlsplit(log["uri"]).path))
    data = path.read_bytes()
    shutil.copyfile(path,work/(name+"-systrap-test.log"))
    xml = next(item for item in result["testActionOutput"] if item["name"]=="test.xml")
    shutil.copyfile(Path(unquote(urlsplit(xml["uri"]).path)),work/(name+"-systrap-test.xml"))
    text = data.decode(errors="replace")
    row = report["cells"][name] = {"source":revision,"status":result["status"],"exit":code,
           "logSha256":hashlib.sha256(data).hexdigest(),"cached":result.get("cachedLocally",False),"cases":{},"verified":False}
    for test_name in names:
        counts = row["cases"][test_name] = {}
        for outcome in ("PASS","FAIL","SKIP"):
            counts[outcome] = len(re.findall(r"--- " + outcome + r": Test" + test_name + r" \(",text))
    row["no_child"] = sum("wait(stopped) got error" in line and "no child processes" in line for line in text.splitlines())
    record()
    assert not row["cached"] and code in (0,3)
    for test_name, counts in row["cases"].items():
        assert sum(counts.values()) == runs and not counts["SKIP"], row
        if fixed or test_name != names[0]:
            assert counts["PASS"] == runs, row
    assert row["no_child"] == row["cases"][names[0]]["FAIL"], row
    assert (code == 0) == (row["cases"][names[0]]["FAIL"] == 0), row
    row["verified"] = True
    record()


compare("old-exit-ownership","531bc8fc3f23f845f85762e2ee30bc7c511c4181",False)
compare("revised-exit-ownership",source,True)

record()
test("native",suites)
path=Path("pkg/test/testprocess/testprocess.go")
orig=path.read_bytes()
assert orig.count(b"return path\n")==1
path.write_bytes(orig.replace(b"return path\n",b'return path + ".missing"\n'))
try:
    test("missing",{proc:"StatusWedgedRunscTimesOut"},broken=True)
finally:
    path.write_bytes(orig)
assert not subprocess.check_output(["git","diff","HEAD","--",str(path)])
cross=report["cells"]["arm64_build"]={"elf":{},"verified":False}
record()
subprocess.run([*bazel,"build",*flags,"--config=aarch64",*suites],check=True)
paths=subprocess.check_output([*bazel,"cquery",*flags,"--config=aarch64","--output=files","set("+" ".join(suites)+")"],text=True)
for filename in paths.splitlines():
    f=Path(filename)
    if not f.is_file():
        continue
    with f.open("rb") as stream:
        hdr=stream.read(20)
    if hdr.startswith(b"\x7fELF"):
        assert hdr[4:6]==b"\x02\x01" and int.from_bytes(hdr[18:20],"little")==183
        cross["elf"][f.name]=hashlib.sha256(f.read_bytes()).hexdigest()
assert {owner.split(":")[-1] for owner in suites}==set(cross["elf"]),cross
cross["verified"]=True
record()
