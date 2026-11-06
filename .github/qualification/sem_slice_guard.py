"""Compare a guard on the existing slice with observable analyzer counterexamples."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(os.environ["RUNNER_TEMP"]) / "semaphore-slice-guard"
SOURCE = "874cce4a469d83645caecd06715c28966b1f99c1"
PKG = "//pkg/sentry/kernel/semaphore:"
NOGO = PKG+"semaphore_nogo"
TEST_NOGO = PKG+"semaphore_test_nogo"
OWNER = PKG+"semaphore_test"
report = {"product":SOURCE, "phases":{}}


def save():
    (ROOT/"result.json").write_text(json.dumps(report, indent=2)+"\n")


def run(phase, labels, minutes):
    bep, log = ROOT/(phase+".bep"), ROOT/(phase+".log")
    rows = {label: {"attempts":0} for label in labels}
    rec = {"owners":rows}
    report["phases"][phase] = rec
    save()
    with log.open("w") as out:
        rec["exit"] = subprocess.run([
            "timeout","-k","15",str(minutes*60),str(ROOT/"bazel"),"--nosystem_rc","--nohome_rc",
            "--host_jvm_args=-Xmx2600m","test","--config=x86_64","--keep_going","--jobs=2","--local_resources=memory=9900",
            "--strategy=GoStdlib=processwrapper-sandbox","--strategy=GoStdlibList=processwrapper-sandbox",
            "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
            "--spawn_strategy=linux-sandbox","--strategy=TestRunner=local","--remote_executor=","--remote_cache=","--bes_backend=",
            "--nocache_test_results","--flaky_test_attempts=1","--test_timeout=300","--test_sharding_strategy=disabled",
            "--build_tag_filters=","--test_tag_filters=","--color=no","--curses=no","--noshow_progress","--test_output=errors",
            "--test_env=GO_TEST_WRAP_TESTV=1","--build_event_json_file="+str(bep),*labels
        ],stdout=out,stderr=subprocess.STDOUT).returncode
    save()
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                try:
                    e=json.loads(line)
                except ValueError:
                    continue
                if "started" in e:
                    rec["invocation"]=e["started"].get("uuid")
                label=e["id"].get("testResult",{}).get("label")
                t=e.get("testResult")
                if not t or label not in rows:
                    continue
                row=rows[label];row["attempts"]+=1
                info=t.get("executionInfo",{})
                row.update(status=t.get("status"), strategy=info.get("strategy"),
                           cached=bool(t.get("cachedLocally") or info.get("cachedRemotely")))
                for item in t.get("testActionOutput",[]):
                    if item["name"]!="test.log":
                        continue
                    link=urlsplit(item.get("uri",""))
                    if link.scheme!="file" or link.netloc:
                        continue
                    file=Path(unquote(link.path))
                    if not file.is_file():
                        continue
                    data=file.read_bytes()
                    row["log_sha256"]=hashlib.sha256(data).hexdigest()
                    text=data.decode(errors="replace")
                    if label == OWNER:
                        row["cases"]={name:status for status,name in re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test\w+) ",text)}
                    else:
                        parts=re.split(r"(?m)^(amd64|arm64)\.\.\.",text)
                        row["arch"]={arch:{"passed":body.strip()=="PASS","lines":sorted(set(int(v) for v in re.findall(r"semaphore\.go:(\d+):\d+:",body))),
                            "diagnostics":[line.strip()[:300] for line in body.splitlines() if "checklocks:" in line][:20]} for arch,body in zip(parts[1::2],parts[2::2])}
    rec["errors"]=[]
    if rec["exit"]:
        with log.open(errors="replace") as stream:
            for line in stream:
                if len(rec["errors"])<8 and re.search(r"^ERROR:|fatal error:|panic:|no such file|no such target|permission denied|killed|not permitted",line,re.I) and not re.search(r"https?://|credential|token|api[_-]?key",line,re.I):
                    rec["errors"].append(line.strip()[:360])
    save()
    print(phase,rec["exit"],flush=True)
    return rec


assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
subprocess.run(["git","diff","--exit-code","HEAD"],check=True)
file=Path("pkg/sentry/kernel/semaphore/semaphore.go")
original=file.read_text()
mark=" // +checklocksforce: the slice length is fixed when the set is created."
assert original.count(mark)==1
size_line=original[:original.index(mark)].count("\n")+1
report["size_line"]=size_line
positive=run("guarded",[OWNER,NOGO,TEST_NOGO],44)
try:
    file.write_text(original.replace(mark,""))
    size=run("unforced-size",[NOGO],13)
finally:
    file.write_text(original)

probe="""
func (s *Set) uncheckedIndexedQueueForTest(num int) {
    s.sems[num].waiters.Reset()
}

func (s *Set) uncheckedIndexedValueForTest(num int) {
    s.sems[num].value = 0
}

func (s *Set) uncheckedRetainedMemberForTest(num int) {
    s.mu.Lock()
    queue := &s.sems[num].waiters
    member := &s.sems[num]
    s.mu.Unlock()
    queue.Reset()
    member.value = 0
}
"""
lines={"direct_queue":"s.sems[num].waiters.Reset()", "direct_value":"s.sems[num].value = 0",
       "retained_queue":"queue.Reset()", "retained_value":"member.value = 0"}
report["lines"]={key:original.count("\n")+n for n,line in enumerate(probe.splitlines(),start=1) for key,statement in lines.items() if line.strip()==statement}
try:
    file.write_text(original+probe)
    negative=run("unguarded-index-and-alias",[NOGO],13)
finally:
    file.write_text(original)

def fresh(row):
    return row["attempts"]==1 and row.get("strategy")=="local" and not row.get("cached") and bool(row.get("log_sha256"))

passed = positive["exit"]==0 and all(fresh(row) and row["status"]=="PASSED" for row in positive["owners"].values())
for label in (NOGO,TEST_NOGO):
    parts=positive["owners"][label].get("arch",{})
    passed &= set(parts)=={"arm64","amd64"} and all(v["passed"] for v in parts.values())
passed &= positive["owners"][OWNER].get("cases",{}).get("TestUnregister")=="PASS"
a=size["owners"][NOGO]
parts=a.get("arch",{})
size_ok = fresh(a) and set(parts)=={"arm64","amd64"} and (size["exit"]==0 and all(v["passed"] for v in parts.values()) or size["exit"]==3 and all(v["lines"]==[size_line] for v in parts.values()))
a=negative["owners"][NOGO]
parts=a.get("arch",{})
negative_ok = fresh(a) and negative["exit"]==3 and set(parts)=={"arm64","amd64"} and all({report["lines"]["direct_queue"],report["lines"]["direct_value"]}<=set(v["lines"]) for v in parts.values())
report["verified"]=bool(passed and size_ok and negative_ok)
save()
raise SystemExit(0 if report["verified"] else 1)
