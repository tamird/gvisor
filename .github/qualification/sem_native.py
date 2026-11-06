"""Collect the remaining semaphore evidence independently on one native AMD64 runner."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"]) / "sem-native"
PROD = "5411129f096b976b511b37d7bc58234a0e16cce1"
PKG = Path("pkg/sentry/kernel/semaphore")
OWNER = "//" + str(PKG) + ":semaphore_test"
NOGO = "//" + str(PKG) + ":semaphore_nogo"
EXPECTED = {"SemaphoreTest." + name for name in ("SemOpRemoveWithWaiter", "SemopGetzcnt", "SemopGetzcntOnSetRemoval", "SemopGetzcntOnSignal", "SemopGetncnt", "SemopGetncntOnSetRemoval", "SemopGetncntOnSignal")}
report = {"product": PROD, "phases": {}}


def save():
    (ROOT/"result.json").write_text(json.dumps(report,indent=2)+"\n")


def git(*args):
    return subprocess.check_output(["git",*args],text=True).strip()


def run(phase, label, flags, minutes):
    bep = ROOT/(phase+".bep")
    log = ROOT/(phase+".log")
    rec = report["phases"][phase] = {"label":label,"verified":False}
    save()
    with log.open("w") as out:
        rec["exit"] = subprocess.run(["timeout","-k","15",str(60*minutes),str(ROOT/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx2600m","test",
            "--config=x86_64","--jobs=2","--local_resources=memory=9900","--strategy=GoStdlib=processwrapper-sandbox","--strategy=GoStdlibList=processwrapper-sandbox",
            "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox","--spawn_strategy=linux-sandbox","--strategy=TestRunner=local",
            "--test_tag_filters=","--build_tag_filters=","--remote_executor=","--remote_cache=","--bes_backend=","--nocache_test_results","--flaky_test_attempts=1",
            "--test_sharding_strategy=disabled","--test_timeout=240","--color=no","--curses=no","--noshow_progress","--test_output=errors",
            "--build_event_json_file="+str(bep),*flags,label], stdout=out,stderr=subprocess.STDOUT).returncode
    attempts = []
    for line in bep.open() if bep.exists() else ():
        try:
            event=json.loads(line)
        except ValueError:
            continue
        if "started" in event:
            rec["invocation"]=event["started"]["uuid"]
        if event["id"].get("testResult",{}).get("label")==label and "testResult" in event:
            attempts.append(event["testResult"])
    save()
    if len(attempts)==1:
        a,=attempts
        rec["status"]=a.get("status")
        rec["strategy"]=a.get("executionInfo",{}).get("strategy")
        rec["fresh"]=not (a.get("cachedLocally") or a.get("executionInfo",{}).get("cachedRemotely"))
        for f in a.get("testActionOutput",[]):
            if f["name"] not in ("test.log","test.xml"):
                continue
            u=urlsplit(f.get("uri",""))
            if u.scheme!="file":
                continue
            local=Path(unquote(u.path))
            if not local.is_file():
                continue
            data=local.read_bytes()
            rec[f["name"]+"_sha256"]=sha256(data).hexdigest()
            if f["name"]=="test.log":
                txt=data.decode(errors="replace")
                rec["go"]={n:s for s,n in re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (TestWaiterCheckpointOnThisBuild) ",txt)}
                rec["states"]={e:[int(n),int(v),int(p)] for e,n,v,p in re.findall(r"SEM_STATE (WIRE|GRAPH) active_rejected=true count=(\d+) value=(\d+) pid=(\d+)",txt)}
                rec["gtest"]={n:s for s,n in re.findall(r"\[\s*(OK|FAILED|SKIPPED)\s*\]\s+(SemaphoreTest\.\w+)\s+\(",txt)}
                parts=re.split(r"(?m)^(amd64|arm64)\.\.\.",txt)
                rec["arch"]={k:{"passed":body.strip()=="PASS","lines":sorted(set(int(n) for n in re.findall(r"semaphore\.go:(\d+):\d+:",body)))} for k,body in zip(parts[1::2],parts[2::2])}
            if f["name"]=="test.xml":
                try:
                    document=ET.fromstring(data)
                except ET.ParseError:
                    continue
                rec["gxml"]={x.get("classname","")+"."+x.get("name",""): not any(x.find(k) is not None for k in ("skipped","error","failure")) and x.get("status")!="notrun" for x in document.iter("testcase") if x.get("classname")=="SemaphoreTest"}
    console=log.read_text(errors="replace")
    rec["diagnostics"]=[x[:410] for x in console.splitlines() if re.search(r"ERROR:|fatal error:|undefined:|panic:|permission denied|semaphore(?:_test)?\.go:\d+|killed|no such file|unknown flag:|FAILED_PRECONDITION|g\+\+:|gcc:|exit status",x,re.I)][:20]
    valid=rec.get("fresh") and rec.get("strategy")=="local" and rec.get("test.log_sha256")
    if phase=="positive":
        rec["verified"]=bool(valid and rec["exit"]==0 and set(rec["arch"])=={"amd64","arm64"} and all(v["passed"] for v in rec["arch"].values()))
    elif phase=="negative":
        rec["verified"]=bool(valid and rec["exit"]==3 and set(rec["arch"])=={"amd64","arm64"} and all(report["mutant_lines"]["direct"] in v["lines"] for v in rec["arch"].values()))
    elif phase=="codec":
        rec["verified"]=bool(valid and rec["exit"]==0 and rec["states"]=={"WIRE":[2,1,42],"GRAPH":[2,1,42]} and rec["go"]=={"TestWaiterCheckpointOnThisBuild":"PASS"})
    else:
        xml=rec.get("gxml",{})
        written=rec.get("gtest",{})
        rec["verified"]=bool(valid and rec["exit"]==0 and (xml=={k:True for k in EXPECTED} or written=={k:"OK" for k in EXPECTED}))
    save()
    print(phase,rec["exit"],rec["verified"],rec["diagnostics"][:3],flush=True)


assert git("rev-parse","HEAD")==PROD and not git("diff","--name-only","HEAD")
t=PKG/"semaphore_test.go"; b=PKG/"BUILD"; s=PKG/"semaphore.go"
st=t.read_text(); sb=b.read_text()
try:
    assert st.count('"testing"')==1
    t.write_text(st.replace('"testing"', '"testing"\n "bytes"\n "strings"\n "gvisor.dev/gvisor/pkg/state"')+(ROOT/"sem_same.go.inc").read_text())
    prefix,rule=sb.split('name = "semaphore_test",',1)
    b.write_text(prefix+'name = "semaphore_test",'+rule.replace("    deps = [",'    deps = [\n        "//pkg/state",',1))
    run("codec",OWNER,["--test_env=GO_TEST_WRAP_TESTV=1","--test_arg=-test.v","--test_arg=-test.run=^TestWaiterCheckpointOnThisBuild$"],12)
finally:
    t.write_text(st);b.write_text(sb)
flags=["--test_env=GTEST_FILTER="+":".join(sorted(EXPECTED))]
for variant in ("native","runsc_ptrace"):
    run(variant,"//test/syscalls:semaphore_test_"+variant,flags,18 if variant=="native" else 26)
run("positive",NOGO,[],32)
orig=s.read_text()
probe="""
func (s *Set) uncheckedWaitQueue(num int) {
 s.mu.Lock()
 retained := &s.waiters[num]
 member := &s.sems[num]
 s.mu.Unlock()
 s.waiters[num].Reset()
 retained.Reset()
 member.value = 0
}
"""
report["mutant_lines"]={k:orig.count("\n")+i+1 for i,line in enumerate(probe.splitlines()) for k,w in (("direct","s.waiters[num].Reset()"),("retained","retained.Reset()"),("value","member.value = 0")) if line.strip()==w}
try:
    s.write_text(orig+probe)
    run("negative",NOGO,[],17)
finally:
    s.write_text(orig)
assert not git("diff","--name-only","HEAD")
report["verified"]=all(v["verified"] for v in report["phases"].values())
save()
raise SystemExit(not report["verified"])
