"""Only dispatch linked targets after proving that the failed parser action changed identity."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
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
            "--remote_retries=1", "--invocation_id=" + iid, *flags, *args], stdout=stdout, stderr=stderr)
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
    # The previous carrier preserved the exact object and its NEW execution.
    previous = "c6c88d08-c3f1-4493-8eb0-0cc9c6736795"
    with zipfile.ZipFile(temp/"original-parser-memory.zip") as f:
        provenance = json.loads(f.read("result.json"))
    probe = provenance["stages"]["amd64-identity-control"]
    assert provenance["source"] == report["source"] and probe["status"] == "PASS"
    assert probe["object"]["sha256"] == obj and probe["object"]["bytes"] == 4456624
    assert any(row["link"] == 1 and row["digest"] != orig and row["exit"] == row["code"] == 0 for row in probe["CppCompile"])
    assert previous == provenance["stages"]["linked-x86_64"]["invocation"]
    report["reusedObjectControl"] = {"artifact":13061732767,"digest":probe["CppCompile"][0]["digest"],"object":probe["object"]}
    prevrows = remote(previous)
    def good(r):
        return r.get("stage") in (4, "COMPLETED") and not failed(r)
    # Only previously shared, actually failed/unresolved compiler requests
    # can be affected by the extra diagnostic-only switch. Do not invalidate
    # a unit with a successful output in that same original invocation.
    proved = {normoutput(r["primaryOutputPath"]) for r in prevrows
              if r.get("actionMnemonic") == "CppCompile" and good(r)}
    frontier = [r for r in prevrows if r.get("invocationLinkType") in (2,"MERGED")
                and r.get("actionMnemonic") == "CppCompile" and not good(r)
                and normoutput(r["primaryOutputPath"]) not in proved]
    explicit = [r for r in prevrows if r.get("actionMnemonic") == "CppCompile" and
                "ASTWriterDecl." in r.get("primaryOutputPath","") and failed(r)]
    # A late successful retry under the same digest may already have populated
    # the cache. In that case the failed request no longer needs new flags.
    assert explicit and any(r.get("status",{}).get("code") == 14 for r in explicit)
    def meta(r):
        m=r.get("executedActionMetadata",{})
        msg=r.get("status",{}).get("message","").splitlines()
        msg=next((l[:280] for l in msg if l and not re.search(r"(?i)authorization|header|token|key=|://",l)),None)
        return {"unit":r.get("primaryOutputPath"),"code":r.get("status",{}).get("code",0),"link":r.get("invocationLinkType"),"stage":r.get("stage"),"reason":msg,
                "digest":r.get("actionDigest",{}).get("hash"),"ram":r.get("taskSize",{}).get("estimatedMemoryBytes"),
                "peak":m.get("usageStats",{}).get("peakMemoryBytes"),"started":m.get("executionStartTimestamp"),"finished":m.get("executionCompletedTimestamp")}
    report["previousRequest"]={"invocation":previous,"returned":len(prevrows),"stageLinkCodeCounts":dict(Counter(str((r.get("stage"),r.get("invocationLinkType"),r.get("status",{}).get("code",0))) for r in prevrows)),
             "alreadySucceeded":len(proved),"specificFailure":[meta(r) for r in explicit],"mergedUnresolved":[meta(r) for r in frontier]}
    save()
    newoutputs = {normoutput(r["primaryOutputPath"]) for r in frontier}
    labels = failing_labels | {control} | {canonical(r["targetLabel"]) for r in frontier}
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
    extra = {v[1] for k,v in initial.items() if k[0] in newoutputs}
    assert newoutputs <= {k[0] for k in initial}, "unmapped shared request"
    extra_flag = "-fdiagnostics-show-option"
    if extra:
        more = "(?:" + "|".join(expr(src) for src in sorted(extra)) + ")@" + extra_flag
        allflags += ["--per_file_copt="+more,"--host_per_file_copt="+more]
    report["previousRequest"]["newSources"]=sorted(extra)
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
        if (src in extra) != (argv.count(extra_flag)>initial[k][3].count(extra_flag)):
            offflags.append(src)
        if k in initial and not isparser and src not in deps and src not in extra and initial[k][3]!=argv:
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
        row,_=baz("linked-"+arch,"build",arch,selected,["--keep_going","--remote_download_outputs=toplevel",target],75)
        actual = remote(row["invocation"])
        row["remote"]=summary(actual)
        row["failures"]=[meta(r) for r in actual if failed(r)]
        row["frontierExecutions"]=[meta(r) for r in actual if r.get("actionMnemonic")=="CppCompile" and normoutput(r.get("primaryOutputPath","")) in newoutputs]
        row["cppLinks"]=[meta(r) for r in actual if r.get("actionMnemonic")=="CppLink" and (r.get("targetLabel") or "").endswith(target)]
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
