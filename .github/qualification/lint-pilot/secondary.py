"""Qualify the spelling change's license policy, external visibility and static runtime."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4
import xml.etree.ElementTree as ET

from check import BAZEL, SOURCE, TARGET, TEMP, TREE, events, git, now, require
import qualification_report as qr

STATIC = "da580ac6b929f33b706e6b89c37a77f862ca7844"
WHEEL = "a9c7cef2501c9cfede2110fd6d4e5e62296920efe9abfb84648df866e47f58d1"
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
ROOT = TEMP / "lint-bazel9"
OUTPUT = TEMP / "codespell-secondary.json"
GNU = ["--config=x86_64", f"--inject_repository=tcp_gnu={ROOT}/platform",
       "--host_platform=@tcp_gnu//:gnu", "--extra_execution_platforms=@tcp_gnu//:gnu"]
RECORD = {"source": SOURCE, "staticIntegration": STATIC, "ordinaryOriginal": "41395141116",
          "verdict": "INCOMPLETE", "phases": {}}


def save():
    OUTPUT.write_text(json.dumps(RECORD, indent=2) + "\n")


def digest(data):
    return sha256(data).hexdigest()


def phase(name, body, *extra):
    row = RECORD["phases"][name] = {"verified": False, "start": now(),
                                    "source": git("rev-parse", "HEAD").decode().strip()}
    save()
    try:
        body(row, *extra)
        row["verified"] = True
    except Exception as error:
        row["failure"] = type(error).__name__
        if isinstance(error, ValueError) and len(error.args) == 1:
            # Only our own short gate names are publication candidates.
            message = str(error)
            if re.fullmatch(r"[a-zA-Z0-9 _./+-]{1,130}", message):
                row["gate"] = message
    finally:
        row["end"] = now()
        save()
        print(name, row["verified"], row.get("gate", ""), flush=True)


def command(row, out, args, *, cwd=None, env=None):
    row["invocation"] = str(uuid4())
    args = [args[0], "--invocation_id=" + row["invocation"], *args[1:]]
    p = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "15m", *BAZEL, *args],
                       cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    row.update(exit=p.returncode, commandEnd=now(), logSha256=digest(p.stdout))
    save()
    (out / "arguments").write_text("\n".join(args) + "\n")
    log = p.stdout.decode(errors="replace")
    # Keep original failures discoverable by UUID without publishing raw build logs.
    row["failureClasses"] = sorted({s for s in ("no such target", "no matching toolchains",
        "Unable to find an execution platform", "exec format error", "is forbidden",
        "Permission denied", "RESOURCE_EXHAUSTED", "Error downloading", "No such file or directory")
        if s.lower() in log.lower()})
    save()
    return log


def owner(row, out, target, kind):
    bep = out / "raw.bep"
    es = list(events(bep))
    require(any(e.get("started", {}).get("uuid") == row["invocation"] for e in es) and
            any("finished" in e for e in es), "missing original invocation")
    captured = out / "captured"
    cap = qr.capture(bep, captured, Path("/"), out / "reports", expected_kind=kind)
    ctx = {"expected_source": row["source"], "command_kind": kind, "execution_finished_at": row["commandEnd"]}
    for key, val in {"commit_file":git("cat-file","commit","HEAD"), "source_before":(out/"before").read_bytes(),
                     "source_after":git("status","--porcelain"), "final_head":git("rev-parse","HEAD"),
                     "executed_arguments":(out/"arguments").read_bytes(), "native_exit":str(row["exit"]).encode(),
                     "capture_exit":str(int(bool(cap["errors"]))).encode()}.items():
        (out / key).write_bytes(val)
        ctx[key] = key
    (out / "context").write_text(json.dumps(ctx))
    (out / "expected").write_text(target + "\n")
    verdict = qr.verdict(captured, qr.manifest_path(captured), out / "context", out / "expected")
    row["owner"] = {k:verdict.get(k) for k in ("status","expected_labels","configured_owners")}
    row["captureErrors"] = len(cap["errors"])
    save()
    return es, cap


def licenses(row):
    out = ROOT / "licenses"
    out.mkdir()
    catalog = Path("tools/licensecheck/dependencies.yaml")
    policy = Path("governance/licensing.yaml")
    before = [digest(f.read_bytes()) for f in (catalog, policy)]
    block = catalog.read_text().split("\n- dependency: codespell\n", 1)[1].split("\n- dependency:", 1)[0]
    exception = policy.read_text().split("  - dependency: codespell\n", 1)[1].split("  - dependency:", 1)[0]
    licenses = ["CC-BY-SA-3.0", "GPL-2.0-only"]
    row.update(wheel=WHEEL, catalogLicenses=re.findall(r"^    - (\S+)$", block, re.M),
               exceptionLicenses=re.findall(r"^      - (\S+)$", exception, re.M))
    require("sha256: "+WHEEL in block and
            "commit: 193cd7d27cd571f79358af09a8fb8997e54f8fff" in block and
            licenses == row["catalogLicenses"] == row["exceptionLicenses"], "unexpected audited wheel or license exception")
    env = dict(os.environ, MAKEFLAGS="DOCKER_BUILD=false BAZEL="+BAZEL[0], PATH=str(ROOT)+":"+os.environ["PATH"])
    trace = subprocess.check_output(["make","-sn","mod","OPTIONS=","TARGETS=graph --output=json"], env=env, text=True)
    require(BAZEL[0]+" mod " in trace and "docker exec" not in trace, "nested Make selected another Bazel")
    target = "//tools/licensecheck/main:licensecheck"
    log = command(row, out, ["run", *GNU, "--build_event_json_file="+str(out/"raw.bep"), target, "--", "--mode=verify"], env=env)
    row["records"] = [int(n) for n in re.findall(r"^all (\d+) dependencies have up-to-date license entries that conform to governance/licensing.yaml$", log, re.M)]
    row["problems"] = [re.sub(r"https?://[^\s\x22]+", "<url>", s)[:300] for s in log.splitlines()
        if re.match(r"^(missing entry for |duplicate entry for |stale entry for |[\w.+/@-]+ (was audited |has (no|unknown|invalid) license)|licenses of |licensecheck: [0-9]+ problems)", s)][:100]
    row["unchanged"] = before == [digest(f.read_bytes()) for f in (catalog, policy)]
    row["verifierStartFailure"] = "cannot run make mod" in log
    es = list(events(out / "raw.bep"))
    require(any(e.get("started",{}).get("uuid")==row["invocation"] for e in es) and
            any(e.get("id",{}).get("targetCompleted",{}).get("label")==target and
                e.get("completed",{}).get("success") for e in es), "missing license checker executable completion")
    require(row["exit"] == 0 and len(row["records"]) == 1 and row["records"][0] > 0 and row["unchanged"], "canonical verify failed")


def tests(row, label, dispatch):
    out = ROOT / ("dispatch" if dispatch else "governance")
    out.mkdir()
    (out / "before").write_bytes(git("status","--porcelain"))
    extra = ["--test_arg=-v"] if dispatch else ["--test_env=GO_TEST_WRAP_TESTV=1"]
    command(row, out, ["test", *GNU, "--nocache_test_results", "--test_sharding_strategy=disabled",
        "--modify_execution_info=^TestRunner$=+no-remote-cache", "--strategy=TestRunner=remote",
        "--remote_download_regex=.*[/]test[.](log|xml)$", "--build_event_json_file_path_conversion=false",
        "--build_event_json_file="+str(out/"raw.bep"), "--execution_log_json_file="+str(out/"spawns"), *extra, label])
    es, cap = owner(row, out, label, "test")
    attempts = [e["testResult"] for e in es if e.get("id",{}).get("testResult",{}).get("label")==label]
    row["attempts"] = [r.get("status") for r in attempts]
    launches = [s for s in events(out/"spawns") if s.get("mnemonic")=="TestRunner" and s.get("targetLabel")==label and
                not any(a.endswith("/generate-xml.sh") or a=="--generate-xml" for a in s["commandArgs"])]
    row["launches"] = [{"runner":s.get("runner"),"cached":s.get("cacheHit",False),"exit":s.get("exitCode",0),
                        "platform":allowed_platform(s)} for s in launches]
    reports = {r["name"]:r for r in cap["reports"] if r["identity"]["label"]==label}
    row["reports"] = {name:r["sha256"] for name,r in reports.items()}
    save()
    require(set(reports)=={"test.log","test.xml"}, "missing original test log or XML")
    xml = list(ET.parse(out/reports["test.xml"]["path"]).iter("testcase"))
    log = (out/reports["test.log"]["path"]).read_text(errors="replace")
    row["xmlCounts"] = {"cases":len(xml),"skipped":sum(x.find("skipped") is not None or x.get("result")=="suppressed" or x.get("status")=="notrun" for x in xml),
                        "failed":sum(x.find("failure") is not None or x.find("error") is not None for x in xml)}
    if dispatch:
        names = ["test_formatter_status","test_diagnostics_and_continuation"]
        row["named"] = [n for n in names if re.search(r"^"+n+r"\b.*\.\.\. ok$",log,re.M)]
        require(row["named"] == names and "Ran 2 tests" in log, "missing original lint dispatch methods")
    else:
        row["named"] = [x.get("name") for x in xml if x.get("name")=="TestLicensePolicy"]
        require(row["named"] == ["TestLicensePolicy"], "missing executed catalog policy case")
    require(len(launches)==1 and row["launches"][0]["runner"]=="remote" and not row["launches"][0]["cached"] and
            row["launches"][0]["platform"].get("container-image")=="docker://"+os.environ["GNU"], "missing fresh GNU remote test")
    require(row["owner"]["status"]=="PASS" and row["attempts"]==["PASSED"] and len(xml)>0 and
            row["xmlCounts"]["failed"]==row["xmlCounts"]["skipped"]==row["exit"]==0, "nonpassing test owner")


def downstream(row):
    source = Path.cwd()
    out = ROOT / "downstream"
    out.mkdir()
    (out/"MODULE.bazel").write_text('module(name="consumer")\nbazel_dep(name="gvisor", version="0.0.1")\n'
        +'local_path_override(module_name="gvisor",path='+json.dumps(str(source))+')\n')
    (out/"BUILD.bazel").touch()
    def mapping(name):
        p=subprocess.run([*BAZEL,"mod","dump_repo_mapping",name], cwd=out, capture_output=True)
        require(p.returncode==0, "external repo mapping failed")
        return json.loads(p.stdout)
    rootmap=mapping("")
    deps=mapping(rootmap["gvisor"])
    excluded={"codespell","lint_inputs","cpplint","buildifier_prebuilt"}
    row.update(rootModule="consumer", externalRepo=rootmap["gvisor"], gvisorMappings=len(deps),
               leaked=sorted(excluded.intersection(deps)), rootLeaks=sorted(excluded.intersection(rootmap)))
    hashes=json.loads((out/"MODULE.bazel.lock").read_bytes())["registryFileHashes"]
    row["formatterRegistryFiles"]=[key for key in hashes if any("/modules/"+n+"/" in key for n in excluded)]
    row["registryHashCount"]=len(hashes)
    # A public source file exercises the external repo without selecting an optional lint action.
    log=command(row,out,["build","--nobuild","--build_event_json_file="+str(out/"raw.bep"),"@gvisor//:LICENSE"],cwd=out)
    es=list(events(out/"raw.bep"))
    row["configured"]=[e["id"]["targetConfigured"]["label"] for e in es if "configured" in e and "targetConfigured" in e.get("id",{})]
    row["knownGazelleRootPolicy"]='" tag in a non-root Bazel module is forbidden' in log and '"go_deps.' in log
    row["publicSourceAnalyzed"]=row["exit"]==0 and any(x.endswith("//:LICENSE") for x in row["configured"])
    require(bool(deps) and bool(hashes) and not row["leaked"] and not row["rootLeaks"] and not row["formatterRegistryFiles"], "optional lint dependency in external graph")
    require(row["publicSourceAnalyzed"] or row["knownGazelleRootPolicy"], "unexplained external target result")


def allowed_platform(spawn):
    return {p["name"]:p["value"] for p in spawn["platform"]["properties"] if p["name"] in
            ("Arch","OSFamily","workload-isolation-type","container-image","EstimatedMemory")}


def scratch(row, arch):
    out = ROOT / arch
    out.mkdir()
    (out/"before").write_bytes(git("status","--porcelain"))
    platform="@cpplint_qualification//:scratch_"+arch
    command(row,out,["build","--inject_repository=cpplint_qualification="+str(ROOT/"scratch"),
        "--host_platform="+platform, "--extra_execution_platforms="+platform,
        "--platforms=//tools/bazeldefs:linux_"+("arm64" if arch=="amd64" else "amd64"),"--config=static-python",
        "--modify_execution_info=^Codespell$=+no-cache","--build_event_json_file="+str(out/"raw.bep"),
        "--execution_log_json_file="+str(out/"spawns"),TARGET])
    owner(row,out,TARGET,"build")
    spawns=[s for s in events(out/"spawns") if s.get("mnemonic")=="Codespell" and s.get("targetLabel")==TARGET]
    row["spawns"]=len(spawns)
    require(len(spawns)==1,"missing spelling action")
    s,=spawns
    inputs={p["path"]:p.get("digest",{}).get("hash") for p in s["inputs"]}
    args=s["commandArgs"]
    native=args[args.index("-IB")-1]
    row.update(physical=allowed_platform(s), runner=s.get("runner"),cached=s.get("cacheHit",False),
               program=native,programSha256=inputs[native],actionExit=s.get("exitCode",0))
    save()
    require(row["physical"]=={"Arch":arch,"OSFamily":"linux","container-image":IMAGE,"workload-isolation-type":"oci"}
            and row["runner"]=="remote" and not row["cached"], "wrong execution platform or cached spelling action")
    src={p.decode() for p in git("ls-files","-z").split(b"\0") if p and p.endswith((b".c",b".cc",b".h",b".go",b".md",b".html"))}
    actual={path.split("/ROOT.d/",1)[1]:h for path,h in inputs.items() if "/ROOT.d/" in path}
    end=next(i for i,a in enumerate(args) if a.endswith("/spelling.ok"))
    row.update(sourceCount=len(actual),membership=digest("\0".join(sorted(actual)).encode()),
        matchesSource=actual=={p:digest(Path(p).read_bytes()) for p in src},
        matchesArgv=len(args[end+1:])==len(src) and set(args[end+1:])==src)
    require(row["matchesSource"] and row["matchesArgv"] and row["sourceCount"]==3329 and
        row["membership"]=="df598334c271ca01d2a443d570d00053d6be8868f0b616bfce8fc9e6a1f00635","changed or incomplete spelling input set")
    exe=Path(subprocess.check_output([*BAZEL,"info","execution_root"],text=True).strip())/native
    elf=exe.read_bytes()
    info=subprocess.check_output(["readelf","-hldW",str(exe)],text=True)
    row.update(machine=int.from_bytes(elf[18:20],"little"), interpreter="INTERP" in info,needed="(NEEDED)" in info)
    require(native.startswith("external/") and "musl" in native and "cpplint_qualification" not in native and
            elf.startswith(b"\x7fELF") and digest(elf)==row["programSha256"] and row["machine"]=={"amd64":62,"arm64":183}[arch]
            and not row["interpreter"] and not row["needed"], "wrong interpreter ELF")
    require(any(o["path"].endswith("/spelling.ok") and o.get("digest") for o in s.get("actualOutputs",[])) and
            row["exit"]==row["actionExit"]==0 and row["owner"]["status"]=="PASS","spelling build did not pass")


def main():
    require(git("rev-parse","HEAD","HEAD^{tree}").decode().splitlines()==[SOURCE,TREE] and
            not git("status","--porcelain"), "wrong initial product source")
    phase("licenseVerify",licenses)
    phase("governance",tests,"//tools/licensecheck:licensecheck_test",False)
    phase("dispatch",tests,"//tools:lint_test",True)
    phase("external",downstream)
    require(not git("status","--porcelain") and git("rev-parse",STATIC+"^").decode().strip()==SOURCE,
            "wrong source after product checks")
    git("checkout","--detach","--quiet",STATIC)
    require(set(git("diff","--name-only",SOURCE,"HEAD").decode().splitlines())=={".bazelrc","MODULE.bazel"},
            "unexpected static composition")
    for arch in ("amd64","arm64"):
        phase("scratch-"+arch,scratch,arch)
    require(not git("status","--porcelain"), "qualification modified the product")
    if all(r["verified"] for r in RECORD["phases"].values()) and len(RECORD["phases"])==6:
        RECORD["verdict"]="PASS"
    RECORD["verifiedAt"]=now()
    save()
    return RECORD["verdict"]!="PASS"


if __name__=="__main__":
    raise SystemExit(main())
