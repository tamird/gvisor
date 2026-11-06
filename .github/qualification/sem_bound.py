"""Distinguish runtime owner identity and two independent Checklocks results."""
import json
import os
from pathlib import Path
import re
import subprocess

root=Path(os.environ["RUNNER_TEMP"])/"sem-member"
root.mkdir(exist_ok=True)
case="pkg/sentry/kernel/semaphore"
code=Path(case,"semaphore.go")
test=Path(case,"semaphore_test.go")
report={"source":"43fddadc7eb86b4056201b4f64cacc7ad4beaabf","cases":{}}


def save():
    (root/"member.json").write_text(json.dumps(report, indent=2)+"\n")


def bazel(phase,label,*args):
    r={"label":label};report["cases"][phase]=r;save()
    log=root/(phase+".log")
    with log.open("w") as out:
        r["exit"]=subprocess.run([str(root/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx3g",
            "test","--config=x86_64","--jobs=3","--strategy=TestRunner=local","--test_sharding_strategy=disabled",
            "--nocache_test_results","--test_output=all",*args,label],stdout=out,stderr=subprocess.STDOUT).returncode
    original=Path("bazel-testlogs",case,label.split(":")[-1],"test.log")
    if original.is_file():
        text=original.read_text(errors="replace")
        r["bound"]=re.findall(r"SEM_MEMBER bound=(\d+) rebound=(\d+) size=(\d+)",text)
        r["cases"]=re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (TestSemaphoreOwner)",text)
        parts=re.split(r"(?m)^(amd64|arm64)\.\.\.",text)
        r["sections"]={a:{"passed":sec.strip()=="PASS","lines":[l.strip()[:400] for l in sec.splitlines()
            if "semaphore.go:" in l][:55]} for a,sec in zip(parts[1::2],parts[2::2])}
    if r["exit"]:
        r["diagnostics"]=[l.strip()[:370] for l in log.read_text(errors="replace").splitlines()
            if re.search(r"undefined:|error:|\.go:\d+:|permission denied|no such file",l,re.I)][:22]
    save()


assert subprocess.check_output(["git","rev-parse","HEAD^"],text=True).strip()==report["source"]
before=test.read_text()
test.write_text(before.replace('"testing"','"testing"\n "fmt"\n "unsafe"')+'''
func TestSemaphoreOwner(t *testing.T) {
 ctx:=contexttest.Context(t)
 r:=NewRegistry(auth.NewRootUserNamespace())
 s,err:=r.FindOrCreate(ctx,123,3,0600,true,true,true)
 if err!=nil {t.Fatal(err)}
 for i:=range s.sems {if s.sems[i].mu!=&s.mu {t.Fatalf("creation owner %d",i)}; s.sems[i].mu=nil}
 s.afterLoad(ctx)
 for i:=range s.sems {if s.sems[i].mu!=&s.mu {t.Fatalf("load owner %d",i)}}
 fmt.Printf("SEM_MEMBER bound=%d rebound=%d size=%d\\n",len(s.sems),len(s.sems),unsafe.Sizeof(sem{}))
}
''')
bazel("identity","//"+case+":semaphore_test","--test_arg=-test.v","--test_arg=-test.run=^TestSemaphoreOwner$")
test.write_text(before)
label="//"+case+":semaphore_nogo"
bazel("positive",label)
old=code.read_text()
probe="""
func (s *Set) retainedMemberProbe(num int) {
 s.mu.Lock()
 member := &s.sems[num]
 s.mu.Unlock()
 member.value = 0
 member.waiters.Reset()
}
"""
lines={str(old.count("\n")+i+1):line.strip() for i,line in enumerate(probe.splitlines()) if "member.value" in line or "member.waiters" in line}
code.write_text(old+probe)
bazel("unlocked",label)
code.write_text(old)
report["negative_source_lines"]=lines
identity=report["cases"]["identity"]
positive=report["cases"]["positive"].get("sections",{})
negative=report["cases"]["unlocked"].get("sections",{})
adequate=identity["exit"]==0 and bool(identity.get("bound")) and set(positive)==set(negative)=={"amd64","arm64"}
for arch,section in negative.items():
    hits="\n".join(section["lines"])
    adequate &= all(re.search(r"semaphore\.go:"+line+r"[:,]",hits) is not None for line in lines)
report["observed"]=bool(adequate)
save()
raise SystemExit(not adequate)
