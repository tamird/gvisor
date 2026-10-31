"""Only dispatch linked targets after proving that the failed parser action changed identity."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

from parser_mem_common import call

temp = Path(os.environ["RUNNER_TEMP"])
dest = temp / "compiler-owners"
target = "//tools/nvidia_driver_differ:driver_ast_parser"
old = "41d0faaf-0ed2-474b-93d7-6f9745ae03f4"
orig = "3aed290e7e6fba7d4bc3be56479288d8ac3ac91a1a18f36fafdbb37355e0b4b9"
obj = "ab3fe6511710d41b897d4db9606855883ef189d4c2df951ba1e917cebf030cb3"
flag = "-fno-color-diagnostics"
report = {"status": "FAIL", "source": os.environ["PRODUCT"], "original": old, "stages": {}}
def save():
    (dest / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

def diagnose(path):
    lines = path.read_text(errors="replace").splitlines()
    return [s[:600] for s in lines if not any(k in s.lower() for k in
        ("header=", "key=", "token=", "bearer", "authorization", "https://", "grpc://", "grpcs://"))
        and any(k in s for k in ("ERROR:", "FAILED:", "Resource Exhausted", "Build did NOT",
            "Invalid", "failed to", "parse option", "Elapsed time:", "processes:", "Build completed"))][-25:]

def baz(stage, cmd, arch, flags, args, minutes):
    iid = str(uuid4())
    out, err = (temp / (stage + "." + ext) for ext in ("out", "err"))
    row = report["stages"][stage] = {"invocation": iid, "status": "INCOMPLETE"}; save()
    start = time.monotonic()
    with out.open("wb") as stdout, err.open("wb") as stderr:
        ret = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", f"{minutes}m", "bazelisk",
            "--bazelrc=" + str(temp / "images.bazelrc"), cmd, "--config=" + arch, "--color=no", "--curses=no",
            "--remote_retries=0", "--invocation_id=" + iid, *flags, *args], stdout=stdout, stderr=stderr)
    row.update(bazelExit=ret.returncode, elapsed=round(time.monotonic()-start, 2), diagnostics=diagnose(err),
               status="PASS" if not ret.returncode else "FAIL")
    save()
    print(stage, row["status"], row["bazelExit"], iid, flush=True)
    return row, out

def remote(iid, label=None):
    sel = {"invocationId": iid}
    if label:
        sel["targetLabel"] = label
    return call("GetExecution", executionLookup=sel).get("execution", [])

def failed(r):
    return r.get("status", {}).get("code", 0) or r.get("exitCode", 0)

def summary(rows):
    fails = [r for r in rows if failed(r)]
    return {"returned":len(rows), "failureCounts":dict(Counter(
            f'{r.get("actionMnemonic","?")}:RPC{r.get("status",{}).get("code",0)}:exit{r.get("exitCode",0)}' for r in fails)),
        "recentFailures":[{"mnemonic":r.get("actionMnemonic"),"label":r.get("targetLabel"),
            "output":r.get("primaryOutputPath"),"link":r.get("invocationLinkType"),
            "peak":r.get("executedActionMetadata",{}).get("usageStats",{}).get("peakMemoryBytes")}
            for r in fails[-14:]]}

def normoutput(s):
    return s.split("/bin/", 1)[-1].removesuffix(".d").removesuffix(".o").removesuffix(".pic")

def canonical(s):
    if s.startswith("@") and not s.startswith("@@"):
        return "@" + s
    return s

def aquery(stage, labels, flags):
    q = 'mnemonic("CppCompile",set(' + ' '.join(sorted(labels)) + '))'
    r, out = baz(stage, "aquery", "x86_64", flags,
        ["--output=jsonproto","--noinclude_artifacts","--include_commandline",q], 9)
    assert r["bazelExit"] == 0, stage
    data = json.loads(out.read_bytes())
    lab = {str(x["id"]):x["label"] for x in data["targets"]}
    result = {}
    for a in data.get("actions",()):
        argv = a.get("arguments", [])
        if not ("-c" in argv and "-o" in argv):
            continue
        output = argv[argv.index("-o")+1]
        result[(normoutput(output),output.split("/bin/",1)[0])] = (
            lab[str(a["targetId"])],argv[argv.index("-c")+1],a.get("executionPlatform",""),argv)
    return result

def exactelf(path, arch, linked):
    with path.open("rb") as f:
        hdr = f.read(64); f.seek(0)
        h = hashlib.sha256()
        while buf := f.read(2**20):
            h.update(buf)
    typ, machine = int.from_bytes(hdr[16:18], "little"), int.from_bytes(hdr[18:20], "little")
    assert hdr[:6] == b"\x7fELF\x02\x01" and machine == {"x86_64":62,"aarch64":183}[arch], (path,hdr[:20])
    assert typ in ((2,3) if linked else (1,)), (path,typ)
    if linked:
        assert int.from_bytes(hdr[24:32],"little"), "zero executable entry"
    return {"file":path.name,"sha256":h.hexdigest(),"bytes":path.stat().st_size,"ELF e_type":typ,"ELF e_machine":machine}

def run():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == report["source"]
    rows = remote(old)
    killed = [r for r in rows if r.get("actionMnemonic")=="CppCompile" and r.get("status",{}).get("code")==8]
    failed_outputs = {normoutput(r["primaryOutputPath"]) for r in killed}
    failing_labels = {canonical(r["targetLabel"]) for r in killed}
    assert target in failing_labels and len(failed_outputs)>2
    # Reuse the original ordinary platform, with a sibling only for the parser.
    # Previously healthy libraries keep the normal platform and command line.
    build = temp / "gnu/BUILD"
    with build.open("a") as f:
        f.write('\nconstraint_setting(name="parser_memory", visibility=["//visibility:public"])\n'
                'constraint_value(name="large",constraint_setting=":parser_memory", visibility=["//visibility:public"])\n'
                'platform(name="parser_large",parents=[":gnu"],constraint_values=[":large"],'
                'exec_properties={"EstimatedMemory":"8GB"},visibility=["//visibility:public"])\n')
    reg = ["--extra_execution_platforms=@epoll_gnu//:gnu,@epoll_gnu//:parser_large",
           "--experimental_add_exec_constraints_to_targets=//tools/nvidia_driver_differ:driver_ast_parser$=@epoll_gnu//:large"]
    controls = sorted({canonical(r["targetLabel"]) for r in rows if r.get("actionMnemonic")=="CppCompile"
        and (r.get("targetLabel") or "").startswith("@") and "//llvm:" in r.get("targetLabel","")
        and not failed(r) and canonical(r["targetLabel"]) not in failing_labels})
    assert controls
    control = next((l for l in controls if l.endswith(":Demangle")),controls[0])
    guarded = aquery("platform-routing-preflight",{target,control},reg)
    assert any(v[0].endswith(target) and v[2].endswith(":parser_large") for v in guarded.values())
    assert any(not v[0].endswith(target) and v[2].endswith(":gnu") for v in guarded.values())
    probe, _ = baz("amd64-identity-control","build","x86_64",[*reg, "--strategy_regexp=RISCVTargetParserDef[.]inc=linux-sandbox"],["--output_groups=compilation_outputs",target],15)
    rr = remote(probe["invocation"],target)
    recs=probe["CppCompile"]=[{"digest":r.get("actionDigest",{}).get("hash"),"link":r.get("invocationLinkType",0),
        "code":r.get("status",{}).get("code",0),"exit":r.get("exitCode",0),
        "peakBytes":r.get("executedActionMetadata",{}).get("usageStats",{}).get("peakMemoryBytes")}
        for r in rr if r.get("actionMnemonic")=="CppCompile" and
        r.get("primaryOutputPath","").endswith("/driver_ast_parser.pic.d")]
    if not probe["bazelExit"]:
        matches=list(Path("bazel-bin/tools/nvidia_driver_differ/_objs/driver_ast_parser").glob("driver_ast_parser*.o"))
        assert len(matches)==1
        probe["object"]=exactelf(matches[0],"x86_64",False)
    save()
    assert probe["bazelExit"]==0 and probe["object"]["sha256"]==obj
    assert any(r["digest"] and r["digest"]!=orig and r["code"]==r["exit"]==0 and r["link"] in (1,"NEW") for r in recs), "missing distinct successful NEW action"
    labels = failing_labels | {control}
    initial = aquery("original-owner-commands", labels, [])
    # BuildBuddy's primary artifact is the .d next to the actual .o. Restrict
    # the diagnostic-only option to TUs with a ResourceExhausted receipt.
    matches = {k[0]:v for k,v in initial.items() if k[0] in failed_outputs}
    missing = failed_outputs - matches.keys()
    report.update(originalOOMAttempts=len(killed),originalOOMUnits=len(failed_outputs),
                  originalOOMOwners=sorted(failing_labels),unclassifiedOOmUnits=sorted(missing),
                  healthyControl=control)
    save()
    assert len(matches)>1 and not missing, "unclassified historical C++ outputs"
    deps = {v[1] for v in matches.values() if not v[0].endswith(target)}
    assert deps
    # Canonical labels may use ':' for the package separator. Retain the entire
    # path within the source package and prove actual membership below.
    def expr(src):
        root = re.sub(r"^external/[^/]+/", "",src)
        return "(?:^|/|:)" + "[:/]".join(re.escape(p) for p in root.split("/")) + "$"
    pattern = "(?:" + "|".join(expr(src) for src in sorted(deps)) + ")@" + flag
    allflags = [*reg, "--per_file_copt="+pattern, "--host_per_file_copt="+pattern]
    after = aquery("selected-owner-commands", labels, allflags)
    offplatform, offflags, ordinary_changes = [], [], []
    selected_units = set()
    for k,v in after.items():
        lab,src,plat,argv = v
        isparser=lab.endswith(target)
        if not plat.endswith(":parser_large" if isparser else ":gnu"):
            offplatform.append([lab,plat])
        if (src in deps) != (argv.count(flag) >
                                   max((a[3].count(flag) for a in [initial.get(k)] if a),default=0)):
            offflags.append(src)
        if src in deps:
            selected_units.add(src)
        if k in initial and not isparser and src not in deps and initial[k][3]!=argv:
            ordinary_changes.append(src)
    report["selection"]={"beforeActions":len(initial),"afterActions":len(after),"sameOutputCoordinates":len(initial.keys()&after.keys()),
        "selectedCppSources":len(deps),"verifiedSelectedCppSources":len(selected_units),"wrongPlatforms":offplatform[:8],
        "wrongPerFileFlags":offflags[:8],"unselectedArgvChanges":ordinary_changes[:8]}
    save()
    assert not offplatform and not offflags and not ordinary_changes and deps==selected_units
    assert set(after)==set(initial), "configuration changed output paths of uninvolved compilations"
    # TableGen can use the local strategy previously demonstrated. This time
    # select its one failing output only; all other generated includes stay eligible for remote cache.
    selected = [*allflags, "--strategy_regexp=RISCVTargetParserDef[.]inc=linux-sandbox",
                "--remote_exec_header=x-buildbuddy-platform.estimatedmemory=8000000000"]
    for arch in ("x86_64","aarch64"):
        row,_=baz("linked-"+arch,"build",arch,selected,[target],55)
        row["remote"]=summary(remote(row["invocation"]))
        if row["bazelExit"]==0:
            row["binary"]=exactelf(Path("bazel-bin/tools/nvidia_driver_differ/driver_ast_parser"),arch,True)
        save()
        if row["bazelExit"] and not report["stages"].get("linked-aarch64") and arch=="x86_64":
            # Do not start a second expensive linked request on the same failure.
            break
    if "binary" in report["stages"]["linked-x86_64"]:
        r,_=baz("amd64-parser-test","test","x86_64",selected,
            ["--test_output=errors",target+"_test"],18)
        save()
    report["status"]="PASS" if (all("binary" in report["stages"].get("linked-"+a,{}) for a in ("x86_64","aarch64")) and
        report["stages"].get("amd64-parser-test",{}).get("bazelExit")==0) else "FAIL"
    save()

save()
try:
    run()
except Exception as exc:
    report["failure"]=f"{type(exc).__name__}: {str(exc)[:350]}"
    save()
    raise
sys.exit(report["status"]!="PASS")
