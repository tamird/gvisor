"""Run the three original dependency policies and two directed violations."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse

W = Path(os.environ["RUNNER_TEMP"]) / "deps-policy"
STOCK = "a39f38e89dc7110a5bc35cd70821388d2d22f62a"
FIX = "dbb62f477d7abed652ae5505ea714436e0c138d5"
F = "//test/rbe/deps_policy_fixture:"
OWNERS = ["//pkg/tcpip:netstack_deps_test", "//runsc:runsc_deps_test", "//runsc/cmd/sentry:gvisor_sentry_deps_test"]
ROOTS = "set(" + " ".join(OWNERS) + ")"
P = {"mixed":"source-presubmit-mixed","arm":"source-presubmit-arm64","amd":"source-presubmit","custom":"source-presubmit", "race":"source-presubmit-mixed"}
REPO = re.compile(r"//[a-zA-Z0-9_/.-]+:[a-zA-Z0-9_.+-]+")
TERMS = ("Traceback", "not a valid", "has no", "expected", "not allowed", "undeclared", "no such",
    "invalid", "not defined", "not visible", "no matching", "must be", "incompatible", "unrecognized",
    "error in", "Build did NOT complete", "out of memory", "internal error", "failed to load", "requires")
r = {"source":{"stock":STOCK,"fixed":FIX},"queries":{},"tests":{}}
out = W/"result.json"
EXPR = '''def format(target):
    o = build_options(target)
    race = [str(v) for k,v in o.items() if str(k).endswith("//go/config:race")]
    return "\\t".join([str(target.label), str(o.get("//command_line_option:cpu")), str(o.get("//command_line_option:platforms")), str(o.get("//command_line_option:host_platform")), ",".join(race)])
'''

def save():
    raw=json.dumps(r,indent=1,sort_keys=True)
    assert len(raw)<240000
    out.write_text(raw+"\n")

def call(command,phase,profile,args):
    flags=["--config="+P[profile], "--spawn_strategy=sandboxed", "--jobs=12","--color=no","--curses=no"]
    if profile in ("custom","race"):flags+=["--config=race"]
    if profile=="custom":flags+=["--platforms="+F+"custom_platform","--extra_execution_platforms="+F+"custom_platform"]
    if command=="test":flags+=["--strategy=TestRunner=sandboxed","--build_event_json_file="+str(W/(phase+".bep")),
        "--nocache_test_results","--flaky_test_attempts=1","--keep_going","--test_output=errors"]
    cmd=[str(W/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx8200m",command,*flags,*args]
    log=W/(phase+".log"); stdout=W/(phase+".stdout")
    with log.open("wb") as err, stdout.open("wb") as so:
        try:status=subprocess.run(cmd,stdout=so,stderr=err,timeout=650).returncode
        except subprocess.TimeoutExpired:status=124
    text=log.read_text(errors="replace")
    locs=[]
    for line in text.splitlines():
        if not any(x in line for x in ("ERROR:","Error:","Error in")):continue
        matches=re.findall(r"(?:^|/)([A-Za-z0-9_.-]+(?:\.bzl|BUILD(?:\.bazel)?)):(\d+):(\d+)",line)
        locs.extend(list(m) for m in matches if m not in locs)
    return status, {"terms":[t for t in TERMS if t.lower() in text.lower()],"locations":locs[:15]}

def platform(s):
    for name in ("linux_amd64","linux_arm64","custom_platform","host_platform","host:host"):
        if name in s:return name
    return "other"

def query(src,profile,extra=False):
    phase=src+"_"+profile+("_aux" if extra else "")
    expr="labels(targets,"+ROOTS+")"
    if extra:expr="labels(targets,labels(arm64_policy,"+ROOTS+"))"
    status,signals=call("cquery", phase, profile, ["--output=starlark","--starlark:file="+str(W/"format.cquery"),expr])
    rows=[]
    for raw in (W/(phase+".stdout")).read_text().splitlines():
        f=raw.split("\t")
        if len(f)!=5:continue
        name=f[0].lstrip("@")
        if REPO.fullmatch(name):
            rows.append([name, f[1] if f[1] in ("k8","aarch64","x86_64") else "other",
                platform(f[2]),platform(f[3]),f[4] in ("True","true")])
    r["queries"][phase]={"exit":status,"rows":sorted(rows),"signals":signals};save()

def tests(src,profile,labels):
    phase=src+"_tests_"+profile
    file=W/(phase+".targets");file.write_text("\n".join(labels)+"\n")
    status,signals=call("test",phase,profile,["--target_pattern_file="+str(file)])
    t={"exit":status,"signals":signals,"results":{}};r["tests"][phase]=t;save()
    bep=W/(phase+".bep")
    if not bep.is_file():return
    for line in bep.open():
        e=json.loads(line); i=e["id"].get("testResult",{});label=i.get("label")
        if label not in labels or "testResult" not in e:continue
        rr=e["testResult"];info=rr.get("executionInfo",{})
        row={"status":rr.get("status"), "cached":bool(rr.get("cachedLocally") or info.get("cachedRemotely")),
             "strategy":info.get("strategy") if info.get("strategy") in ("linux-sandbox","processwrapper-sandbox","local","sandboxed") else "other"}
        t["results"].setdefault(label,[]).append(row)
        for o in rr.get("testActionOutput",[]):
            if o.get("name")!="test.log":continue
            uri=urlparse(o.get("uri",""))
            if uri.scheme!="file":continue
            path=Path(unquote(uri.path))
            if not path.exists() or path.stat().st_size>100000:continue
            data=path.read_bytes()
            h=hashlib.sha256(data).hexdigest()
            if o.get("digest") and o["digest"]!=h:continue
            txt=data.decode(errors="replace")
            row["log_sha256"]=h
            row["arm_heading"]="ARM64 dependencies:" in txt
            row["denied"]=[d for d in ("denied_arm","denied_amd","race_off","ordinary","allowed_arm","allowed_amd") if F+d in txt]
        save()

def checkout(src):
    sha=STOCK if src=="stock" else FIX
    subprocess.run(["git","checkout","-q","--detach",sha],check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==sha
    # Only this independent synthetic package is overlaid on the tested source.
    q=Path("test/rbe/deps_policy_fixture");q.mkdir(exist_ok=True)
    fixture=(W/"fixture.BUILD").read_text()
    if src=="stock":
        fixture=fixture.replace('"presubmit_deps_test")','presubmit_deps_test = "deps_test")',1)
    (q/"BUILD").write_text(fixture)
    (q/"nodes.bzl").write_bytes((W/"nodes.bzl").read_bytes())

def assess_custom():
    labels={F+"custom_policy_test", F+"race_policy_test", F+"reject_arm_test"}
    for src in ("stock","fixed"):
        phase=src+"_tests_custom"
        result=r["tests"][phase]
        if result["results"].keys()!=labels or result["exit"]!=0:return False
        for rows in result["results"].values():
            if len(rows)!=1 or rows[0]["status"]!="PASSED" or rows[0]["cached"] or not rows[0].get("log_sha256"):return False
    return True

save()
for src in ("stock","fixed"):
    checkout(src)
    tests(src,"custom",[F+"custom_policy_test", F+"race_policy_test", F+"reject_arm_test"])
r["verified"]=assess_custom();save()
raise SystemExit(0 if r["verified"] else 1)
