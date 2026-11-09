"""Run the exact permanent TCP option cases and the owning TCP analyzers."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"])
WORK = ROOT/"tcp-queued-gso"
OUT = WORK/"results"
TOOLS = ROOT/"packet-worker"
sys.path.insert(0,str(TOOLS))
import timer_teardown as public

BASE = "a4ca95b85a9a770e6e01852c30da57ae94f89b52"
FIX = "546bf896c12cecef4dc70093643ce963c41ff6a4"
TREE = "f243fb0cfa2c820766e74bccb082b9dd44b95d42"
PATH = "pkg/tcpip/transport/tcp/test/e2e/tcp_test.go"
TCP = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
NOGO = "//pkg/tcpip/transport/tcp:tcp_nogo"
PARENTS = ("TestSetExperimentOption", "TestSetExperimentOptionIPv6")
DISABLED = "TestSetExperimentOptionWithOptionDisabled"
CASES = {p+"/host_GSO="+b for p in PARENTS for b in ("false","true")} | {PARENTS[1]+"/untagged_host_GSO",DISABLED}
BAD = PARENTS[1]+"/host_GSO=true"
REPORT = {"verified":False,"carrier":os.environ["GITHUB_SHA"],"cells":{},"veth_artifact":13644693514,"public_inputs":public.seen}
def git(*args):
    return subprocess.check_output(["git",*args])
def save():
    (OUT/"result.json").write_text(json.dumps(REPORT,indent=2,sort_keys=True)+"\n")
def original(events,label,phase):
    event, = [e for e in events if "testResult" in e]
    ident,result = event["id"]["testResult"],event["testResult"]
    row = REPORT["cells"][phase]
    assert ident["label"].removeprefix("@@")==label
    row.update(config=ident["configuration"]["id"],status=result["status"],files={})
    for f in result["testActionOutput"]:
        name=f["name"]
        if name not in ("test.xml","test.log"): continue
        url=urlsplit(f["uri"]); assert url.scheme=="file" and not url.netloc
        path=Path(unquote(url.path))
        assert path.samefile(Path("bazel-testlogs")/label[2:].replace(":","/")/name)
        data=path.read_bytes(); digest=hashlib.sha256(data).hexdigest()
        assert 0<len(data)<=8<<20 and int(f.get("length",len(data)))==len(data)
        assert not f.get("digest") or f["digest"]==digest
        (OUT/(phase+"-"+name)).write_bytes(data)
        row["files"][name]=[len(data),digest]
    save()
    assert set(row["files"])=={"test.xml","test.log"}
    assert any((c:=e["id"].get("targetCompleted",{})).get("label","").removeprefix("@@")==label and c.get("configuration",{}).get("id")==row["config"] for e in events)
    assert all(int(ident.get(k,1))==1 for k in ("attempt","run","shard"))
    exec_info=result.get("executionInfo",{})
    assert exec_info.get("strategy") in ("local","standalone") and not result.get("cachedLocally") and not exec_info.get("cachedRemotely")
    return (OUT/(phase+"-test.log")).read_text().replace("\x16",""),ET.parse(OUT/(phase+"-test.xml"))
def assess(phase,text,xml):
    row=REPORT["cells"][phase]
    assert not any(e.tag=="skipped" for e in xml.iter())
    if phase=="nogo":
        parts=re.split(r"(?m)^(amd64|arm64)\.\.\.",text)
        assert len(parts)==5 and set(parts[1::2])=={"amd64","arm64"} and all(s.strip().splitlines()==["PASS"] for s in parts[2::2])
        assert not any(e.tag in ("failure","error") for e in xml.iter())
        row["architectures"]=parts[1::2]
    else:
        ends=re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (\S+) [(]",text)
        allowed=CASES|set(PARENTS)
        assert len(ends)==len(allowed) and {n for _,n in ends}==allowed
        cases={e.get("name"):e for e in xml.iter("testcase")}
        assert CASES <= cases.keys() <= allowed
        for name in CASES:
            bad=phase=="baseline" and name==BAD
            assert ("FAIL" if bad else "PASS",name) in ends
            assert any(child.tag in ("error","failure") for child in cases[name])==bad
        row["leaves"]={n:s for s,n in ends}
        if phase=="baseline":
            wrong,=re.findall(r"TCP payload at sequence (\d+) has (\d+) bytes, want (\d+)",text)
            assert int(wrong[1])==2*int(wrong[2]) and int(wrong[2])>0
            row["parent_payload_bytes"],row["expected_segment_bytes"]=map(int,wrong[1:])
    assert row["exit"]==(3 if phase=="baseline" else 0) and row["status"]==("FAILED" if phase=="baseline" else "PASSED")
    row["verified"]=True
def main():
    OUT.mkdir(exist_ok=True,parents=True)
    start=git("rev-parse","HEAD").decode().strip()
    assert not git("diff","HEAD","--name-only")
    assert git("rev-parse",FIX+"^").decode().strip()==BASE and git("rev-parse",FIX+"^{tree}").decode().strip()==TREE
    fixture=git("show",FIX+":"+PATH)
    public.assets.DIST.mkdir(parents=True,exist_ok=True)
    public.known.update((r[2],r) for r in json.loads((TOOLS/"inputs.json").read_bytes()))
    public.dirs.update(p for p in (public.assets.DIST/"content").glob("*") if p.is_dir())
    deadline=time.monotonic()+22*60
    try:
        for phase,source,label in (("baseline",BASE,TCP),("candidate",FIX,TCP),("nogo",FIX,NOGO)):
            row=REPORT["cells"][phase]={"verified":False,"source":source,"owner":label}
            save()
            try:
                git("switch","--quiet","--detach",source)
                original_test=Path(PATH).read_bytes()
                try:
                    if phase=="baseline": Path(PATH).write_bytes(fixture)
                    assert Path(PATH).read_bytes()==fixture
                    bep=WORK/(phase+".bep"); console=WORK/(phase+".output")
                    args=[str(TOOLS/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx3g","test","--config=x86_64","-c","opt","--jobs=4",
                          "--lockfile_mode=off","--remote_executor=","--remote_cache=","--bes_backend=","--spawn_strategy=processwrapper-sandbox,local",
                          "--strategy=GoStdlib=processwrapper-sandbox","--strategy=GoStdlibList=processwrapper-sandbox","--strategy=TestRunner=local",
                          "--nocache_test_results","--flaky_test_attempts=1","--test_sharding_strategy=disabled","--test_timeout=300","--build_event_json_file="+str(bep)]
                    if label==TCP:
                        args+=["--test_filter=^("+"|".join((*PARENTS,DISABLED))+")$","--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1"]
                    for attempt in range(12):
                        bep.unlink(missing_ok=True)
                        with console.open("wb") as fd:
                            row["exit"]=subprocess.run(args+["--distdir="+str(p) for p in sorted(public.dirs)]+[label],stdout=fd,stderr=subprocess.STDOUT,timeout=max(1,deadline-time.monotonic())).returncode
                        events=[json.loads(line) for line in bep.read_bytes().splitlines() if line] if bep.exists() else []
                        log=console.read_text(errors="replace");row["diagnostics"]=[s[:400] for s in log.splitlines() if s.startswith("ERROR:") or re.search(r"[.]go:\d+:\d+:",s)][-12:]
                        if any("testResult" in e for e in events) or not public.acquire(log): break
                    assess(phase,*original(events,label,phase))
                finally:
                    if phase=="baseline": Path(PATH).write_bytes(original_test)
            except Exception as exc: row["error"]=repr(exc)[:300]
            save()
    finally:
        git("switch","--quiet","--detach",start)
    REPORT["verified"]=all(row["verified"] for row in REPORT["cells"].values())
if __name__=="__main__":
    try: main()
    except Exception as exc: REPORT["error"]=repr(exc)[:300]
    save()
    raise SystemExit(not REPORT["verified"])
