"""Qualify the relocated compiler caller; do not repeat any Nogo test runners."""

import collections
import hashlib
import json
import os
import unit_matrix
import subprocess
import time
from pathlib import Path

root = Path(os.environ["RUNNER_TEMP"])
out = root / "nogo-receipts"
events = Path(".build_events.json")
bazel = ["bazel", f"--bazelrc={root}/nogo-resources.bazelrc"]
owners = ["//pkg/sentry/loader/vdsodata:vdsodata_nogo", "//pkg/sentry/platform/systrap/sysmsg:sysmsg_nogo"]


def write(name, val):
    (out / (name + ".json")).write_text(json.dumps(val, sort_keys=True, indent=2) + "\n")


def file_hash(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def diag(p):
    with p.open(errors="replace") as f:
        return [s.strip()[:400] for s in f if ("ERROR:" in s or "not found" in s) and not any(t in s.lower() for t in ("token", "http", "bearer"))][:10]


def clean(label):
    repo, sep, rest = label.partition("//")
    return sep+rest if repo in ("@", "@@") else label


def preflight():
    log = root / "preflight.log"
    cmd = [*bazel,"test","--nobuild","--config=buildkite","--config=nogo","//..."]
    with log.open("w") as f:
        rc = subprocess.run(["timeout", "--kill-after=10s", "12m", *cmd],stdout=f,stderr=f).returncode
    diagnostic = {"exit": rc, "errors": diag(log)}
    try:
        selected = unit_matrix.configured_targets(str(events),tests=True,allow_test_no_build=True,expected_exit=rc)
    except (OSError,ValueError) as exc:
        diagnostic["problem"]=str(exc)[:400]
        selected={}
    keep = sorted(selected)
    diagnostic.update(configured=keep, verified=rc in (0,1) and set(owners)<=set(keep))
    write("preflight",diagnostic)
    return keep if diagnostic["verified"] else []


def actions(mode, roots):
    # Query the *explicit* configured owners of the real wildcard. Aliases may
    # forward to an implementation rule with a different action owner label.
    q = "deps(set("+" ".join(roots)+"))"
    args = [*bazel,"aquery",f"--strip={mode}","--build_tag_filters=","--include_aspects","--output=jsonproto","--noinclude_artifacts","--noinclude_commandline",q]
    p = root / f"graph-{mode}.json"
    log = root / f"graph-{mode}.log"
    with p.open("w") as f, log.open("w") as err:
        rc = subprocess.run(["timeout","--kill-after=10s","12m",*args],stdout=f,stderr=err).returncode
    r={"exit":rc,"errors":diag(log),"roots":len(roots)}
    if rc:
        write("analysis-"+mode,r)
        return None
    a=json.loads(p.read_bytes())
    labels={str(t["id"]):clean(t["label"]) for t in a.get("targets",[])}
    groups=collections.defaultdict(list)
    for action in a.get("actions",[]):
        groups[labels.get(str(action.get("targetId")),"(none)"),action["mnemonic"]].append(action.get("actionKey",""))
    example=[label for label in labels.values() if "/loader/vdsodata:" in label or "/systrap/sysmsg:" in label]
    types=collections.Counter(v["mnemonic"] for v in a.get("actions",[]))
    r.update(actionCount=sum(types.values()),exampleOwners=sorted(set(example)),mnemonics=types,
             missingKeys=sum(1 for v in a.get("actions",[]) if not v.get("actionKey")),
             rootOwners=sum(l in set(roots) for l in set(labels.values())))
    write("analysis-"+mode,r)
    return ({k:sorted(v) for k,v in groups.items()},
            bool(groups and example and types["GoStaticAnalysis"] and types["TestRunner"] and not r["missingKeys"]))


def executable(target, arm, flags):
    log=root/f"{arm}.log"
    with log.open("w") as f:
        rc=subprocess.run(["timeout","--kill-after=10s","8m",*bazel,"build","--strip=never",*flags,target],stdout=f,stderr=f).returncode
    r={"target":target,"exit":rc,"errors":diag(log)}
    if rc:
        r["incompatible"]=[l.strip()[:400] for l in log.read_text(errors="replace").splitlines() if "incompatible" in l.lower() or "target platform" in l.lower() or "@platforms//cpu:" in l][:8]
        write(arm,r)
        return r
    files=subprocess.check_output([*bazel,"cquery","--strip=never",*flags,"--output=files",target],text=True,stderr=subprocess.DEVNULL).splitlines()
    paths=[Path(name) for name in files if Path(name).is_file() and Path(name).read_bytes()[:4]==b"\x7fELF"]
    assert paths and len({file_hash(x) for x in paths})==1, files
    exe=paths[0]
    header=exe.read_bytes()[:20]
    program=subprocess.check_output(["readelf","-lW",str(exe)],text=True)
    dynamic=subprocess.check_output(["readelf","-dW",str(exe)],text=True)
    r.update(arch=int.from_bytes(header[18:20],"little"),interpreter=[l.strip() for l in program.splitlines() if "Requesting program interpreter:" in l],
             needed=[l.strip() for l in dynamic.splitlines() if "(NEEDED)" in l],sha256=file_hash(exe))
    if arm=="static-amd64":
        r["smoke"]=[]
        for call in ("0","1","2"):
            p=subprocess.run([str(exe),"--loops","10","--syscall",call],capture_output=True,text=True,timeout=10)
            r["smoke"].append({"call":call,"exit":p.returncode,"stdout":p.stdout.strip()})
    write(arm,r)
    return r


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==os.environ["PRODUCT"]
    started=int(time.time())
    # Full package loading, before the first compiler action. Use checkable
    # configured roots, rather than the broad tags regex that also matched -nogo.
    all_log=root/"load.log"
    with all_log.open("w") as f:
        rc=subprocess.run(["timeout","--kill-after=10s","8m",*bazel,"query","//...","--output=package"],stdout=subprocess.DEVNULL,stderr=f).returncode
    write("package-loading",{"exit":rc,"errors":diag(all_log)})
    assert rc==0
    roots=preflight()
    target="//images/benchmarks/syscallbench:syscallbench"
    cc="//tools/cstate_wake_probe:cstate_wake_probe"
    arm=["--platforms=@llvm//platforms:linux_aarch64"]
    st=executable(target,"static-amd64",[])
    na=executable(target,"incompatible-arm64",arm)
    gnu=[executable(cc,"gnu-amd64",[]),executable(cc,"gnu-arm64",arm)]
    ok=(st.get("arch")==62 and not st.get("interpreter") and not st.get("needed") and
        len(st.get("smoke",[]))==3 and all(x["exit"]==0 and "# Executed 10 calls" in x["stdout"] for x in st["smoke"]) and
        na["exit"]==1 and any("incompatible" in x.lower() for x in na.get("incompatible",[])) and
        all(v.get("arch")==m and any("ld-linux" in e for e in v["interpreter"]) and any("libc.so.6" in e for e in v["needed"]) for v,m in zip(gnu,[62,183])))
    write("link-verdict",{"verified":bool(ok),"source":os.environ["PRODUCT"],"compiler":"a385bd2b0"})
    a=actions("sometimes",roots) if roots else None
    b=actions("never",roots) if roots else None
    if a and b:
        a, valid_a = a
        b, valid_b = b
        changed={str(k):[len(a.get(k,[])),len(b.get(k,[]))] for k in a.keys()|b.keys() if a.get(k)!=b.get(k)}
        types=collections.Counter(k[1] for k in a.keys()|b.keys() if a.get(k)!=b.get(k))
        write("changes",{"groups":changed,"differenceMnemonics":types})
    result={"verified":bool(ok and a and b and valid_a and valid_b),"source":os.environ["PRODUCT"],"compiler":"a385bd2b0","nogo":"6aa894aa1",
            "configured":len(roots),"started":started,"finished":int(time.time()),"staticAndGNU":bool(ok),
            "stripGroupsChanged":len(changed) if a and b else None}
    write("verdict",result)
    assert result["verified"],result


if __name__ == "__main__":
    main()
