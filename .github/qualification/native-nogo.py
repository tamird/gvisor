"""Qualify the relocated compiler caller; do not repeat any Nogo test runners."""

import collections
import hashlib
import json
import os
import shutil
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


def actions(mode):
    # Only Nogo roots: test-specific rc flags govern selection in the actual
    # wildcard, so reproduce that selection in the query rather than asking
    # for every build action beneath //... .
    q = 'deps(tests(attr("tags", "nogo", //...)))'
    cmd = [*bazel, "aquery", f"--strip={mode}", "--output=jsonproto", "--noinclude_artifacts", "--noinclude_commandline", q]
    p = root / f"action-{mode}.json"
    log = root / f"action-{mode}.log"
    with p.open("w") as f, log.open("w") as err:
        rc = subprocess.run(["timeout", "--kill-after=10s", "8m", *cmd], stdout=f, stderr=err).returncode
    write("analysis-"+mode,{"exit":rc, "errors":diag(log)})
    assert rc == 0, (mode,rc)
    a = json.loads(p.read_bytes())
    labels = {t["id"]: t["label"] for t in a["targets"]}
    selected = sorted({v for v in labels.values() if v.endswith("_nogo") and not v.startswith("@")})
    groups = collections.defaultdict(list)
    for act in a["actions"]:
        groups[labels.get(act.get("targetId"),"(none)"),act["mnemonic"]].append(act.get("actionKey",""))
    assert all(l in selected for l in owners)
    return {k:sorted(v) for k,v in groups.items()}, selected


def smoke(arm, flags):
    target = "//images/benchmarks/syscallbench:syscallbench"
    log = root / (arm + "-static.log")
    with log.open("w") as f:
        rc = subprocess.run(["timeout","--kill-after=10s","9m",*bazel,"build","--strip=never", *flags,target], stdout=f,stderr=f).returncode
    r = {"exit":rc, "diagnostics":diag(log)}
    if rc != 0:
        with log.open(errors="replace") as f:
            r["linkDiagnostics"] = [l.rstrip()[:450] for l in f if ("unable to find" in l or "undefined symbol" in l or "cannot find" in l or "ld.lld: error" in l)][:8]
    else:
        exe = Path("bazel-bin/images/benchmarks/syscallbench/syscallbench")
        header = exe.read_bytes()[:20]
        program = subprocess.check_output(["readelf","-lW",str(exe)],text=True)
        dynamic = subprocess.check_output(["readelf","-dW",str(exe)],text=True)
        p = subprocess.run([str(exe),"--loops","10"],capture_output=True,text=True,timeout=10)
        r.update(arch=int.from_bytes(header[18:20],"little"),noInterpreter="INTERP" not in program,noNeeded="(NEEDED)" not in dynamic,sha256=file_hash(exe),smokeExit=p.returncode,smokeOutput=p.stdout.strip())
        r["verified"]=r["arch"]==62 and r["noInterpreter"] and r["noNeeded"] and r["smokeExit"]==0 and "# Executed 10 calls" in p.stdout
    write(arm+"-static",r)
    return r


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==os.environ["PRODUCT"]
    start = int(time.time())
    # The package error must be resolved before any compile or analyzer runs.
    root_label = root / "nogo-query.txt"
    qerr = root / "query.err"
    with root_label.open("w") as f, qerr.open("w") as err:
        rc = subprocess.run(["timeout","--kill-after=10s","6m",*bazel,"query","--output=label",'tests(attr("tags","nogo",//...))'],stdout=f,stderr=err).returncode
    labels = sorted(root_label.read_text().splitlines())
    write("query",{"status":rc,"configuredLabels":labels,"errors":diag(qerr)})
    assert rc==0 and set(owners)<=set(labels), (rc,diag(qerr))

    # Record ordinary source behavior before using the declared target sysroot
    # as an independent control. No overlay, apt installation, cache, or RBE.
    default = smoke("ordinary",[])
    if not default.get("verified"):
        smoke("musl-control",["--platforms=@llvm//platforms:linux_x86_64_musl"])

    a, roots_a = actions("sometimes")
    b, roots_b = actions("never")
    changed={str(k): [len(a.get(k,[])),len(b.get(k,[]))] for k in a.keys()|b.keys() if a.get(k)!=b.get(k)}
    classes=collections.Counter(k[1] for k in a.keys()|b.keys() if a.get(k)!=b.get(k))
    write("changes",{"ownerMnemonics":changed,"graphTargets":roots_b})
    sumry={"verified":bool(default.get("verified") and roots_a==roots_b),
           "source":os.environ["PRODUCT"],"compiler":"f8c678f63","nogo":"6aa894aa1","queryRoots":len(labels),
           "oldActions":sum(map(len,a.values())),"newActions":sum(map(len,b.values())),
           "nogoGraphLabels":len(roots_b),"differenceMnemonics":classes,"differentGroups":len(changed),
           "started":start,"finished":int(time.time()),"ordinaryStatic":default.get("verified",False)}
    write("verdict",sumry)
    assert sumry["verified"],sumry


if __name__ == "__main__":
    main()
