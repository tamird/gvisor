"""Check one native test and describe the configured ARM source presubmit."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

from image_execution import execution_records


WORK = Path(os.environ["RUNNER_TEMP"]) / "arm64-namespace-presubmit"
OUT = WORK / "results"
SOURCE = "127967d3179491381f2a85e4804ae8236fc89e35"
OWNER = "//test/syscalls:uname_test_native"
PLATFORM = "//tools/bazeldefs:linux_arm64"
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
PUB = re.compile(r"//[A-Za-z0-9_./+-]*:[A-Za-z0-9_./+-]+")
HEX = re.compile(r"[a-f0-9]{64}")
CONFIGS = ("Sanity","SetNames","UnprivilegedSetNames","UnshareUTS")
TERMS = ("no registered executors", "no executors", "not enough capacity", "no matching executors",
         "no available executors", "no matching execution platform", "failed to schedule",
         "failed precondition","failed_precondition","deadline exceeded","deadline_exceeded","timed out",
         "unavailable", "resource exhausted", "resource_exhausted", "canceled", "cancelled",
         "permission denied","unauthenticated", "unknown option", "unrecognized option", "no such target",
         "segmentation fault", "no such file or directory", "illegal instruction","exec format error",
         "operation not permitted", "outofmemoryerror","no space left on device",
         "missing input file", "command not found", "action failed", "killed", "firecracker")
BASE = ["bazelisk", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx9500m"]
CASES = set(CONFIGS)


def read(path):
    if path.is_file():
        return json.loads(path.read_text())
    return {}


def save(name, info):
    data = json.dumps(info, sort_keys=True, indent=1)
    assert len(data)<150000 and not re.search(r"(?i)x-buildbuddy-api-key|authorization:|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}", data)
    (OUT / (name + ".json")).write_text(data + "\n")


def signals(text):
    text = text.lower()
    return sorted(word for word in TERMS if word in text)


def events(file):
    if file.is_file():
        with file.open() as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except ValueError:
                    return


def status(log):
    if not log.is_file():
        return {}
    err = set()
    codes = set()
    with log.open(errors="replace") as stream:
        for line in stream:
            if "[0 / 1]" in line or len(line)>12500:
                continue
            err.update(signals(line))
            codes.update(re.findall(r"\b(?:UNAVAILABLE|DEADLINE_EXCEEDED|FAILED_PRECONDITION|RESOURCE_EXHAUSTED|PERMISSION_DENIED|UNAUTHENTICATED|INTERNAL)\b", line))
    return {"diagnostics": sorted(err), "grpc":sorted(codes)}


def write_profile():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    assert Path(".bazelversion").read_text().strip()=="9.3.1"
    base = Path(".bazelrc").read_text().splitlines()
    rc = [l.replace(":source-presubmit ",":arm-source ") for l in base if ":source-presubmit " in l and not l.startswith("#")]
    changes = {"--config=x86_64":"--config=aarch64","--extra_execution_platforms=//tools/bazeldefs:linux_amd64":
               "--extra_execution_platforms="+PLATFORM, "--remote_default_exec_properties=Arch=amd64":"--remote_default_exec_properties=Arch=arm64"}
    for old,new in changes.items():
        assert sum(l.endswith(" "+old) for l in rc)==1,old
        rc = [l[:-len(old)]+new if l.endswith(" "+old) else l for l in rc]
    # The test command takes one owner. The inventory adds back the original
    # target-pattern file explicitly, preserving the same selection flags.
    rc.remove("test:arm-source --target_pattern_file=test/rbe/source-presubmit.targets")
    assert "test:arm-source --strategy=TestRunner=remote" in rc
    assert "build:arm-source --spawn_strategy=remote --noremote_local_fallback" in rc
    assert f"build:arm-source --remote_default_exec_properties=container-image={IMAGE}" in rc
    (WORK / "profile.bazelrc").write_text("\n".join(rc)+"\n")


def bazel(mode, name, extra, minutes, secret=False):
    cfg = ["--bazelrc="+str(WORK/"profile.bazelrc")]
    if secret:
        cfg.append("--bazelrc="+str(WORK/"remote.bazelrc"))
    bep = WORK/(name+".bep")
    with (WORK/(name+".out")).open("w") as out, (WORK/(name+".log")).open("w") as err:
        code=subprocess.run(["timeout","--signal=INT","--kill-after=40s",f"{minutes}m",*BASE,*cfg,mode,
           "--config=arm-source","--color=no","--curses=no","--build_event_json_file="+str(bep),*extra],stdout=out,stderr=err).returncode
    return code


def remote():
    # This runs in the test's sandbox, before the unmodified syscall runner.
    witness = """printf 'ARM_NAMESPACE_CPU=%s\\n' "$(uname -m)"
if [ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ]; then echo ARM_NAMESPACE_CGROUP_V2=yes; fi
if grep -iq firecracker /sys/class/dmi/id/{product_name,sys_vendor} /proc/device-tree/compatible /sys/firmware/devicetree/base/compatible 2>/dev/null; then echo ARM_NAMESPACE_GUEST_ID=firecracker; fi
exec "$@" """
    code=bazel("test","native",["--jobs=55","--remote_download_outputs=minimal",
            "--remote_download_regex=.*testlogs/test/syscalls/uname_test_native/test[.](log|xml)",
            "--remote_timeout=120s","--flaky_test_attempts=1","--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_output=errors",
            "--execution_log_json_file="+str(WORK/"native.spawn"),
            "--run_under="+shlex.join(["/bin/bash","-c",witness,"arm-namespace"]),OWNER],34,True)
    save("native_status",{"source":SOURCE,"bazel_exit":code})


def collect():
    r=read(OUT/"native_status.json")
    r.update(verified=False, owner=OWNER, source=SOURCE, test_results=[], test_actions=[], failure_owners=[])
    r.update(status(WORK/"native.log"))
    summary=[]
    failures=[]
    for e in events(WORK/"native.bep"):
        ids=e.get("id",{})
        if "started" in e: r["invocation"]=str(uuid.UUID(e["started"]["uuid"]))
        rid=ids.get("testResult",{})
        if rid.get("label")==OWNER and "testResult" in e:
            t=e["testResult"]; info=t.get("executionInfo",{})
            r["test_results"].append({"passed":t.get("status")=="PASSED","remote":info.get("strategy")=="remote",
                     "cached":bool(t.get("cachedLocally") or info.get("cachedRemotely")),"attempt":rid.get("attempt",1)})
        if ids.get("testSummary",{}).get("label")==OWNER and "testSummary" in e:
            summary.append(e["testSummary"].get("overallStatus")=="PASSED")
        if "action" in e:
            aid=ids.get("actionCompleted",{})
            if not e["action"].get("success", False):
                own=aid.get("label","")
                fs=e["action"].get("failureDetail",{})
                words=signals(json.dumps(fs))
                mnemonic=e["action"].get("type","")
                failures.append({"owner": own if PUB.fullmatch(own) else "dependency",
                                 "mnemonic":mnemonic if re.fullmatch(r"[A-Za-z0-9_]{1,65}",mnemonic) else "other",
                                 "diagnostics":words})
        if "finished" in e: r["finished_passed"]=e["finished"].get("exitCode",{}).get("name")=="SUCCESS"
    r["failure_owners"]=failures[:12]
    r["summary"]=summary
    save("native",r)
    p=WORK/"native.spawn"
    if p.exists():
        try:
            with p.open(encoding="utf-8",newline="") as stream:
                for _,a in execution_records(stream):
                    if not a or a.get("mnemonic")!="TestRunner" or a.get("targetLabel")!=OWNER: continue
                    argv=a.get("commandArgs",[])
                    if not any(Path(s).name=="test-setup.sh" for s in argv) and not any(s.endswith("/test.cache_status") for s in a.get("listedOutputs",[])): continue
                    props={q["name"]:q["value"] for q in a.get("platform",{}).get("properties",[])}
                    r["test_actions"].append({"aarch64_worker":props.get("Arch")=="arm64","ubuntu":props.get("container-image")==IMAGE,
                        "firecracker":props.get("workload-isolation-type")=="firecracker","root":props.get("dockerUser")=="root",
                        "ipv6":props.get("network-enable-ipv6")=="true","remote":a.get("runner")=="remote","cached":bool(a.get("cacheHit")),
                        "qemu":any("qemu-" in s for s in argv),"exit":a.get("exitCode",0)})
        except ValueError:
            r["partial_spawn"]=True
    save("native",r)
    testdir=Path("bazel-testlogs/test/syscalls/uname_test_native")
    text=""
    log=testdir/"test.log"
    if log.is_file() and log.stat().st_size<2**22: text=log.read_text(errors="replace")
    r["cpu_reported_aarch64"]=bool(re.search(r"(?m)^ARM_NAMESPACE_CPU=aarch64\s*$",text))
    r["cgroup2_observed"]="ARM_NAMESPACE_CGROUP_V2=yes" in text
    r["firecracker_guest_marker"]="ARM_NAMESPACE_GUEST_ID=firecracker" in text
    r["cases"]={}
    xml=testdir/"test.xml"
    if xml.is_file() and xml.stat().st_size<2**22:
        try:
            for case in ET.parse(xml).iter("testcase"):
                name=case.get("name","")
                end=re.split(r"[._/]",name)[-1]
                if end not in CASES: continue
                fail=case.find("failure") is not None or case.find("error") is not None
                skip=(case.find("skipped") is not None or case.get("result")=="skipped" or case.get("status")=="notrun")
                r["cases"][end]="failed" if fail else "skipped" if skip else "passed"
        except ET.ParseError: r["partial_xml"]=True
    for case in sorted(CASES):
        lines=re.findall(r"(?m)^\[\s*(OK|FAILED|SKIPPED)\s*\]\s+UnameTest\."+case+r"\s+\(",text)
        if lines and case not in r["cases"]:
            r["cases"][case]={"OK":"passed","FAILED":"failed","SKIPPED":"skipped"}[lines[-1]]
    r["verified"]=(r.get("bazel_exit")==0 and summary==[True] and
        r["test_results"]==[{"passed":True,"remote":True,"cached":False,"attempt":1}] and
        any(all(a[k] for k in ("aarch64_worker","ubuntu","firecracker","root","ipv6","remote")) and
            not a["cached"] and not a["qemu"] and not a["exit"] for a in r["test_actions"]) and
        r["cpu_reported_aarch64"] and r["cases"].get("UnshareUTS")=="passed" and set(r["cases"])==CASES)
    save("native",r)


def inventory():
    stage={"verified":False,"source":SOURCE,"phases":{}}
    sets={}
    for role in ("all","tests"):
        opts=["--spawn_strategy=local","--strategy=TestRunner=local","--jobs=4","--nobuild",
              "--target_pattern_file=test/rbe/source-presubmit.targets"]
        if role=="tests": opts.append("--build_tests_only")
        code=bazel("test",role,opts,12)
        conf,done={},{}
        executed=0
        for e in events(WORK/(role+".bep")):
            ids=e.get("id",{})
            if "configured" in e:
                t=ids["targetConfigured"]["label"]
                cfg=[x["targetCompleted"]["configuration"]["id"] for x in e.get("children",[]) if x.get("targetCompleted",{}).get("label")==t and "aspect" not in x["targetCompleted"]]
                kind=e["configured"].get("targetKind","")
                if PUB.fullmatch(t) and len(cfg)==1 and HEX.fullmatch(cfg[0]) and re.fullmatch(r"[a-zA-Z0-9_ ]{1,85}",kind): conf[t,cfg[0]]=kind
            if "aborted" in e and "targetCompleted" in ids and not ids["targetCompleted"].get("aspect"):
                t=ids["targetCompleted"]
                done[t["label"],t.get("configuration",{}).get("id")]=e["aborted"].get("reason")
            executed+=int(any(k in e for k in ("testResult","testSummary","action")))
        selected={t:(cfg,conf[t,cfg]) for (t,cfg),how in done.items() if how=="NO_BUILD" and (t,cfg) in conf}
        stage["phases"][role]={"exit":code,"selected":len(selected),"executions":executed,"complete":bool(
            code==1 and not executed and OWNER in selected and conf.keys()==done.keys() and set(done.values())<={"NO_BUILD","SKIPPED"}),**status(WORK/(role+".log"))}
        sets[role]=selected
        save("inventory",stage)
    # Keep root analysis and execution counts separate. Rule kind comes from
    # Bazel's actual configured target, even when a macro synthesized it.
    selected=sets["tests"]
    assert 1<=len(selected)<1500 and len(sets["all"])<10000
    paths=WORK/"test-owners.aquery"
    paths.write_text('mnemonic("^TestRunner$",set('+" ".join(sorted(selected))+'))\n')
    with (OUT/"owners.tsv").open("w") as dst:
        dst.write("role\tkind\tlabel\n")
        for t,(_,kind) in sorted(sets["all"].items()): dst.write(f"{'run' if t in selected else 'build'}\t{kind}\t{t}\n")
    code=bazel("aquery","shards",["--spawn_strategy=local","--strategy=TestRunner=local","--jobs=4",
            "--output=jsonproto","--include_artifacts=false","--noinclude_commandline","--query_file="+str(paths)],10)
    stage["phases"]["shards"]={"exit":code,**status(WORK/"shards.log")}
    save("inventory",stage)
    if code: return
    graph=json.loads((WORK/"shards.out").read_text())
    targets={str(t["id"]):t["label"] for t in graph["targets"]}
    kinds=Counter(); classes=Counter(); count=Counter(); mismatches=[]; special=[]
    with (OUT/"test_actions.tsv").open("w") as dst:
        dst.write("owner\tclass\ttest_platform\n")
        for a in graph["actions"]:
            t=targets[str(a["targetId"])]
            assert t in selected and a["mnemonic"]=="TestRunner"
            props={"Arch":"arm64","OSFamily":"linux","container-image":IMAGE,"workload-isolation-type":"oci"}
            props.update({p["key"]:p.get("value","") for p in a.get("executionInfo",[])})
            cls=props.get("workload-isolation-type","")
            assert cls in ("oci","firecracker")
            arm=a.get("executionPlatform") in (PLATFORM,"@@"+PLATFORM) and props["Arch"]=="arm64"
            if not arm or props["container-image"]!=IMAGE or props["OSFamily"]!="linux": mismatches.append(t)
            if cls=="firecracker":
                special.append(t)
                if props.get("dockerUser")!="root" or props.get("network-enable-ipv6")!="true": mismatches.append(t)
            count[t]+=1
            classes[cls]+=1
            dst.write(f"{t}\t{cls}\t{'linux_arm64' if arm else 'other'}\n")
    for _,kind in selected.values(): kinds[kind]+=1
    stage.update(runnable=len(selected),build_only=len(sets["all"].keys()-selected.keys()),rule_kinds=dict(kinds),
        shards=sum(count.values()),classes=dict(classes),namespace_owners=sorted(set(special)),mismatches=sorted(set(mismatches))[:20])
    stage["verified"]=(all(stage["phases"][p]["complete"] for p in ("all","tests")) and set(count)==set(selected) and
        set(selected)<=sets["all"].keys() and not mismatches and OWNER in special)
    save("inventory",stage)


if __name__=="__main__":
    OUT.mkdir(exist_ok=True)
    {"profile":write_profile,"remote":remote,"collect":collect,"inventory":inventory}[sys.argv[1]]()
