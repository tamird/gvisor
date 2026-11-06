"""Keep Linux, Sentry, the two analyzer sections and each codec result distinct."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT=Path(os.environ["RUNNER_TEMP"])/"semaphore-queues"
PROD="5411129f096b976b511b37d7bc58234a0e16cce1"
DESTROY="d9f9c4a0bc5caf4a5e7eadf9e2b3cd7ab0590005"
BASE="c4231067d198ea1b38bd629b7c87a5426f44b2c8"
PKG="pkg/sentry/kernel/semaphore"
code=Path(PKG,"semaphore.go")
build=code.with_name("BUILD")
test=code.with_name("semaphore_test.go")
OWNER="//"+PKG+":semaphore_test"
NOGO="//"+PKG+":semaphore_nogo"
NAMES=["SemOpRemoveWithWaiter","SemopGetzcnt","SemopGetzcntOnSetRemoval","SemopGetzcntOnSignal",
       "SemopGetncnt","SemopGetncntOnSetRemoval","SemopGetncntOnSignal"]
GTEST={"SemaphoreTest."+n for n in NAMES}
old_streams={}
report={"product":PROD,"destroy":DESTROY,"base":BASE,"map_comparison":"8ac1eff49e61520224878eea4ef504cce674ecce","phases":{}}


def git(*args):
    return subprocess.check_output(["git",*map(str,args)],text=True).strip()


def save():
    (ROOT/"result.json").write_text(json.dumps(report,indent=2)+"\n")


def rows(phase,labels,flags=()):
    folder=ROOT/phase
    folder.mkdir()
    bep=folder/"bep"
    r={"source_blob":git("hash-object",code),"labels":{}}
    report["phases"][phase]=r
    save()
    with (folder/"log").open("w") as out:
        r["exit"]=subprocess.run(["docker","exec","tcp-compiler",str(ROOT/"bazel"),"--bazelrc="+str(ROOT/"client.rc"),
            "test","--config=x86_64","--jobs=55","--keep_going","--strategy=TestRunner=remote",
            "--noremote_local_fallback","--nocache_test_results","--modify_execution_info=^TestRunner$=+no-remote-cache",
            "--remote_download_outputs=toplevel","--remote_download_regex=.*[/]test[.](log|xml)$",
            "--test_sharding_strategy=disabled","--test_timeout=450","--test_output=errors","--build_event_json_file="+str(bep),
            *flags,*labels],stdout=out,stderr=subprocess.STDOUT).returncode
    save()
    attempts={}
    if bep.is_file():
        for line in bep.open():
            e=json.loads(line)
            if "started" in e: r["invocation"]=e["started"]["uuid"]
            if "testResult" in e: attempts.setdefault(e["id"]["testResult"]["label"],[]).append(e["testResult"])
    for label in labels:
        row={"attempts":len(attempts.get(label,[]))}
        r["labels"][label]=row
        if row["attempts"]!=1: continue
        event=attempts[label][0]; ex=event.get("executionInfo",{})
        row.update(status=event.get("status"),fresh_remote=ex.get("strategy")=="remote" and not ex.get("cachedRemotely") and not event.get("cachedLocally"))
        for name in ("test.log","test.xml") if label.startswith("//test/syscalls:") else ("test.log",):
            path=Path("bazel-testlogs")/label[2:].replace(":","/")/name
            named=[f for f in event.get("testActionOutput",[]) if f.get("name")==name]
            if not path.is_file() or len(named)!=1: continue
            data=path.read_bytes()
            sha=hashlib.sha256(data).hexdigest()
            m=named[0]; uri=m.get("uri","")
            remote=re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$",uri)
            matched=((m.get("digest")==sha and int(m.get("length",len(data)))==len(data)) or
                (remote and remote[1]==sha and int(remote[2])==len(data)) or
                (uri.startswith("file:///") and Path(uri[7:]).is_file() and path.samefile(uri[7:])))
            if not matched: continue
            row[name]=sha;row[name+"_bytes"]=len(data)
            if name=="test.xml":
                row["assertions"]={n.get("classname","")+"."+n.get("name",""):
                    not any(n.find(k) is not None for k in ("failure","error","skipped")) and n.get("status")!="notrun" and n.get("result")!="suppressed"
                    for n in ET.fromstring(data).iter("testcase")}
                continue
            txt=data.decode(errors="replace")
            row["tests"]=dict((n,s) for s,n in re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test\w+)",txt))
            row["old_waiter_count"]=bool(re.search(r"queued waiters after removal = \(5, <nil>\)",txt))
            layout=re.search(r"SEM_LAYOUT member=(\d+) set=(\d+) cycle_allocs=([.\d]+)",txt)
            if layout: row["layout"]={"member":int(layout[1]),"set":int(layout[2]),"cycle_allocations":float(layout[3])}
            for engine,guard,old,num,val,pid,enc in re.findall(r"SEM_STATE (WIRE|GRAPH) active_rejected=(true|false) old=(true|false) count=(\d+) value=(\d+) pid=(\d+) stream=([a-zA-Z0-9+/=]+)",txt):
                if len(enc)>32768: continue
                stream=base64.b64decode(enc,validate=True)
                row.setdefault("codec",{})[engine]={"rejected_waiter":guard=="true","old":old=="true","count":int(num),"value":int(val),"pid":int(pid),"stream_sha256":hashlib.sha256(stream).hexdigest()}
                if phase=="old": old_streams[engine]=enc
            if label==NOGO:
                parts=re.split(r"(?m)^(amd64|arm64)\.\.\.",txt)
                row["sections"]={arch:{"pass":body.strip()=="PASS","source_lines":list(map(int,re.findall(r"semaphore\.go:(\d+):\d+:",body)))}
                    for arch,body in zip(parts[1::2],parts[2::2])}
            row["diagnostic_source_lines"]=re.findall(r"semaphore(?:_test)?\.go:\d+(?::\d+)?",txt)[:18]
    save()
    print(phase,r["exit"],r.get("invocation"),flush=True)


def reset(rev):
    git("restore","--source="+rev,"--worktree","--",code,build,test)
    assert git("hash-object",code)==git("rev-parse",rev+":"+str(code))


def fixture():
    s=test.read_text()
    assert s.count('"testing"')==1
    test.write_text(s.replace('"testing"','"testing"\n "bytes"\n "encoding/base64"\n "fmt"\n "os"\n "strings"\n "unsafe"\n "gvisor.dev/gvisor/pkg/state"')+(ROOT/"sem_case.go").read_text())
    s=build.read_text()
    start=s.index('name = "semaphore_test",')
    before,after=s[:start],s[start:]
    after=after.replace("    deps = [","    deps = [\n        \"//pkg/state\",",1)
    build.write_text(before+after)


assert git("rev-parse","HEAD")==PROD and git("rev-parse","HEAD^")==DESTROY and git("rev-parse","HEAD^^")==BASE
reset(BASE)
git("restore","--source="+DESTROY,"--worktree","--",test)
fixture()
rows("old",[OWNER],["--test_arg=-test.v","--test_arg=-test.run=^(TestUnregister|TestWaiterStorage)$"])
reset(DESTROY)
rows("destroy",[OWNER],["--test_arg=-test.v","--test_arg=-test.run=^TestUnregister$"])
for phase in ("map","indexed"):
    reset(PROD)
    if phase=="map":
        git("apply",ROOT/"queue-map.patch")
        assert git("hash-object",code)=="a14ddad0424f6f7d79532beb0308a99843d5f9e7"
        assert git("hash-object",build)=="7bc7537d6aa7bba9dc35743cb39be29f563861ec"
    fixture()
    rows(phase,[OWNER],["--test_arg=-test.v",*["--test_env=SEM_OLD_"+k+"="+v for k,v in old_streams.items()]])
reset(PROD)
rows("analysis",[NOGO])
syss=["//test/syscalls:semaphore_test_"+variant for variant in ("native","runsc_ptrace")]
rows("syscalls",syss,["--test_env=GTEST_FILTER="+":".join(sorted(GTEST))])
original=code.read_text()
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
source_lines={line.strip():original.count("\n")+i+1 for i,line in enumerate(probe.splitlines()) if "Reset()" in line or "member.value" in line}
code.write_text(original+probe)
rows("unguarded",[NOGO])
code.write_text(original)
report["negative_locations"]=source_lines
assert not git("diff","--name-only","HEAD")
ok=True
for p,r in report["phases"].items():
    ok &= all(x.get("fresh_remote") and x.get("test.log") for x in r["labels"].values())
    if p not in ("old","unguarded"):ok &= r["exit"]==0
for p in ("old","map","indexed"):
    x=report["phases"][p]["labels"][OWNER]
    ok &= x.get("tests",{}).get("TestWaiterStorage")=="PASS" and bool(x.get("layout"))
    ok &= set(x.get("codec",{}))=={"WIRE","GRAPH"} and all(c["rejected_waiter"] and c["old"]==(p!="old") for c in x.get("codec",{}).values())
old=report["phases"]["old"]["labels"][OWNER]
ok &= old.get("old_waiter_count",False) and old.get("tests",{}).get("TestUnregister")=="FAIL"
for p in ("destroy","map","indexed"):
    ok &= report["phases"][p]["labels"][OWNER].get("tests",{}).get("TestUnregister")=="PASS"
for p in ("analysis","unguarded"):
    arches=report["phases"][p]["labels"][NOGO].get("sections",{})
    ok &= set(arches)=={"amd64","arm64"}
    ok &= all(a["pass"] if p=="analysis" else not a["pass"] and source_lines["s.waiters[num].Reset()"] in a["source_lines"] for a in arches.values())
for x in report["phases"]["syscalls"]["labels"].values():
    ok &= set(x.get("assertions",{}))==GTEST and all(x.get("assertions",{}).values())
report["verified"]=bool(ok)
save()
raise SystemExit(not ok)
