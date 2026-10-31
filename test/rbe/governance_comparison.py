"""Run the existing write_source_files test suite on an empty execution worker."""
from datetime import datetime, timezone
import base64
import hashlib
import shutil
import struct
import json
import os
from pathlib import Path
import subprocess
import sys

temp = Path(os.environ["RUNNER_TEMP"])
out = temp / "proof"
out.mkdir(exist_ok=True)
output = out / "governance-comparison.json"
supplier = Path("../supplier").resolve()
target = "//:governance-check"
labels = ["//:governance-regen_0_test", "//:governance-regen_1_test"]
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
stale = "# qualification: deliberately stale CODEOWNERS"
source = "08f421859113f25ca84aef7a0f5f9f457db35e95"
supplier_source = "cac2d7855949d1b222fa26888892fbbe1d31015d"
report = {"expected_arms": ["stock", "fixed", "stale"], "expected_tests": labels,
          "source": source, "supplier": supplier_source, "scratch_image": image, "arms": {}}

def now():
    return datetime.now(timezone.utc).isoformat()

def save():
    output.write_text(json.dumps(report, indent=2) + "\n")

def require(ok, error):
    if not ok:
        raise ValueError(error)

def edit(path, old, new):
    path = Path(path)
    body = path.read_text()
    require(body.count(old) == 1, f"ambiguous replacement in {path}")
    path.write_text(body.replace(old, new))

def sanitise(text):
    return [s[-560:] for s in text.splitlines() if not any(x in s.lower() for x in
            ("api-key", "remote_header", "bes_header", "authorization", "command line", "credential"))]

def control():
    # Change only the existing supplier comparison invocation, not the consumer's map or test suite.
    path = supplier / "lib/private/write_source_file.bzl"
    edit(path, 'load("//lib:paths.bzl"', 'load("@bazel_skylib//rules:native_binary.bzl", "native_test")\nload("//lib:paths.bzl"')
    edit(path, '''        _diff_test(
            name = test_target_name,
            file1 = in_file,
            file2 = out_file,
            failure_message = message,
            diff_args = diff_args,''', '''        native_test(
            name = test_target_name,
            src = Label("@diffutils//:diff"),
            data = [in_file, out_file],
            args = ["-ru"] + diff_args + ["--", "$(rootpath %s)" % in_file, "$(rootpath %s)" % out_file],''')
    with (supplier / "MODULE.bazel").open("a") as fp:
        fp.write('\nbazel_dep(name = "diffutils", version = "3.12")\n')

def static_elf(path):
    elf = path.read_bytes()
    require(elf[:6]==b"\x7fELF\x02\x01", "not an ELF64 file: "+str(path))
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", elf)
    require(header[2] == 62, "not AMD64: "+str(path))
    for offset in range(header[5], header[5]+header[9]*header[10], header[9]):
        require(struct.unpack_from("<I",elf,offset)[0]!=3,"ELF still has PT_INTERP: "+str(path))
    return {"sha256": hashlib.sha256(elf).hexdigest(),"bytes":len(elf),"machine":"AMD64","interpreter":None}

def short_command(argv, timeout):
    try:
        q=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    except subprocess.TimeoutExpired as e:
        q=subprocess.CompletedProcess(argv,124,e.stdout or b"")
    return q.returncode,q.stdout.decode(errors="replace")

def prepare_tools():
    client=str(temp/"native-bazel")
    rc="--bazelrc="+str(temp/"governance.bazelrc")
    status, install = short_command([client,rc,"info","install_base"],100)
    require(status==0, "cannot locate installed tools")
    embedded = Path(install.splitlines()[-1])/"embedded_tools"
    amended = temp/"declared-bazel-tools"
    shutil.copytree(embedded,amended,copy_function=os.link,symlinks=True)
    build=amended/"tools/test/BUILD"
    original=build.read_text()
    embedded_selection='    srcs = select({\n        ":matches_host_constraints": glob(\n            ["posix_tw*"],\n            allow_empty = True,\n        ),\n        "//conditions:default": [":compiled_posix_test_wrapper"],\n    }),'
    require(original.count(embedded_selection)==1,"native POSIX launcher source changed")
    build.unlink() # The copied tree contains hard links to the immutable installation.
    build.write_text(original.replace(embedded_selection,'    srcs = [":compiled_posix_test_wrapper"],'))
    # The module registries pin both release archives below. Supply the same
    # bytes via GNU mirrors; do not wait on an unreachable university mirror.
    cache=temp/"distdir"
    cache.mkdir()
    for pkg,version,sri in [("diffutils","3.12","fIt/n8hgkUH96pzs6FJJ0whiQ5H/Yd7a9Sj8szdyff0="),("sed","4.9","biJrcy4c1zlGStaGK9Ghq6QteYKSLaelNRljHSSXUYE=")]:
        name=f"{pkg}-{version}.tar.xz"
        for mirror in ("https://ftp.gnu.org/gnu","https://mirrors.kernel.org/gnu","https://ftpmirror.gnu.org"):
            ret,_=short_command(["curl","-fLsS","--connect-timeout","4","--max-time","18",f"{mirror}/{pkg}/{name}","-o",str(cache/name)],25)
            if ret==0 and hashlib.sha256((cache/name).read_bytes()).digest()==base64.b64decode(sri):
                break
        else:
            raise ValueError("unable to supply digest-verified "+name)
    opts=[rc,"build","--jobs=4","--spawn_strategy=local","--platforms=//tools/bazeldefs:linux_amd64","--linkopt=-static","--host_linkopt=-static","--features=fully_static_link","--host_features=fully_static_link","--workspace_status_command=","--lockfile_mode=update","--override_repository=bazel_tools="+str(amended),"--distdir="+str(cache),"--host_platform=@governance_workers//:amd64","--extra_execution_platforms=@governance_workers//:amd64"]
    code,log=short_command([client,*opts,"@bazel_tools//tools/test:compiled_posix_test_wrapper"],480)
    require(code==0,sanitise(log)[-14:])
    candidates=list(Path("bazel-bin").glob("external/bazel_tools*/tools/test/compiled_posix_test_wrapper"))
    require(len(candidates)==1,[str(x) for x in candidates])
    report["static_preflight"]=static_elf(candidates[0]); save()
    print("static launcher",report["static_preflight"],flush=True)

def record(arm):
    result = report["arms"][arm] = {"status": "INCOMPLETE", "started_at": now(), "tests": {}, "spawns": []}
    save()
    bep, spawns = temp / f"{arm}.bep", temp / f"{arm}.compact"
    args = [str(temp / "native-bazel"), "--bazelrc=" + str(temp / "governance.bazelrc"), "test", "--jobs=4", "--spawn_strategy=local", "--strategy=TestRunner=remote",
            "--keep_going", "--platforms=//tools/bazeldefs:linux_amd64", "--linkopt=-static", "--host_linkopt=-static", "--noincompatible_exclude_starlark_flags_from_exec_config",
            "--incompatible_use_native_posix_test_wrapper", "--features=fully_static_link", "--host_features=fully_static_link",
            "--workspace_status_command=", "--lockfile_mode=update", "--test_output=errors", "--nocache_test_results",
            "--host_platform=@governance_workers//:amd64", "--extra_execution_platforms=@governance_workers//:amd64",
            "--remote_download_outputs=toplevel", "--execution_log_compact_file=" + str(spawns),
            "--build_event_json_file=" + str(bep), "--nobuild_event_json_file_path_conversion"]
    args.extend(["--override_repository=bazel_tools="+str(temp/"declared-bazel-tools"),"--distdir="+str(temp/"distdir"),"--http_timeout_scaling=0.2"])
    if arm != "stock":
        args.append("--override_module=bazel_lib=" + str(supplier))
    code,raw=short_command([*args,target],720 if arm=="fixed" else 320)
    r=subprocess.CompletedProcess(args,code,raw)
    result.update(exit=r.returncode, execution_finished_at=now())
    logs = sanitise(r.stdout)
    for i, line in enumerate(logs):
        if "Traceback " in line or "ERROR:" in line:
            result["first_error"] = logs[max(0,i-1):i+11]
            break
    result["diagnostics"] = [s for s in logs if any(x in s.lower() for x in
        ("error:", "failed", "such file", "cannot execute", "could not execute", "out of date", "qualification:", "@@ -"))][-18:]
    for line in bep.read_text().splitlines() if bep.exists() else ():
        e = json.loads(line)
        if "started" in e:
            result["invocation"] = e["started"]["uuid"]
        rid = e["id"].get("testResult", {})
        if rid.get("label") in labels:
            result["tests"][rid["label"]] = {"status":e["testResult"]["status"], "cached":e["testResult"].get("cachedLocally",False) or e["testResult"].get("executionInfo",{}).get("cachedRemotely",False)}
    if spawns.exists() and spawns.stat().st_size:
        filters = 'select((.mnemonic // "") == "TestRunner" and ((.targetLabel // "") | startswith("//:governance-regen_"))) | {targetLabel,commandArgs:(.commandArgs[0:4]),platform,runner,status,exitCode,cacheHit}'
        with subprocess.Popen([temp / "bb","print","--compact_execution_log="+str(spawns)], stdout=subprocess.PIPE) as reader:
            rows = subprocess.run(["jq","-c",filters], stdin=reader.stdout, capture_output=True, check=True)
            require(reader.wait()==0, "cannot decode compact actions")
        for line in rows.stdout.splitlines():
            row=json.loads(line)
            if "--generate-xml" in row["commandArgs"]:
                continue
            props={p["name"]:p["value"] for p in row.get("platform",{}).get("properties",[])}
            result["spawns"].append(dict(label=row["targetLabel"],argv=row["commandArgs"],image=props.get("container-image"),
                                         runner=row["runner"],arch=props.get("Arch"),cached=row["cacheHit"] or False,
                                         exit=row["exitCode"] or 0,status=row["status"]))
            binary=Path("bazel-out").resolve().parent/row["commandArgs"][0]
            if binary.is_file():
                result["spawns"][-1]["elf"]=static_elf(binary)
    for label, t in result["tests"].items():
        log = Path("bazel-testlogs") / label.split(":")[1] / "test.log"
        if log.is_file():
            t["log"] = sanitise(log.read_text(errors="replace"))[-20:]
    save()
    print(arm, result["exit"], result.get("invocation"), len(result["spawns"]), json.dumps(result["tests"]), flush=True)
    require(set(result["tests"]) == set(labels), result["diagnostics"])
    require(len(result["spawns"]) == 2 and {s["label"] for s in result["spawns"]} == set(labels), result["spawns"])
    require(all(s["image"] == image and s["arch"] == "amd64" and s["runner"] == "remote" and not s["cached"]
                and "compiled_posix_test_wrapper" in s["argv"][0] and s.get("elf",{}).get("bytes",0)>0 for s in result["spawns"]), result["spawns"])
    require(all(not t["cached"] for t in result["tests"].values()), result["tests"])
    statuses = [result["tests"][l]["status"] for l in labels]
    if arm == "stock":
        require(r.returncode and statuses == ["FAILED", "FAILED"], statuses)
        for label in labels:
            diag="\n".join(result["tests"][label].get("log",[]) + result["diagnostics"]).lower()
            require(any(s in diag for s in ("no such file", "not found", "exec failed", "cannot execute")), diag[-680:])
    elif arm == "fixed":
        require(r.returncode == 0 and statuses == ["PASSED", "PASSED"], statuses)
    else:
        require(r.returncode and statuses == ["FAILED", "PASSED"], statuses)
        log = "\n".join(result["tests"][labels[0]].get("log", []))
        require("@@ -" in log and "+" + stale in log and "CODEOWNERS" in log, log)
    result["status"] = "PASS"

try:
    require(subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip() == source, "source identity mismatch")
    require(subprocess.check_output(["git","-C",supplier,"rev-parse","HEAD"],text=True).strip() == supplier_source, "supplier identity mismatch")
    # The native test launcher is on Bazel 10. Its compiler API cannot load the
    # older Coral toolchain. Select the hosted Linux C++ compiler for all arms;
    # every TestRunner action must still execute remotely on the empty image.
    for arch in ("k8", "aarch64"):
        edit("MODULE.bazel", f'register_toolchains("//:cc_toolchain_{arch}")', "")
    edit("BUILD", '    name = "governance-regen",', '    name = "governance-regen",\n    exec_properties = {"container-image": "'+image+'"},\n    tags = ["no-remote-cache"],')
    # All other actions run on a normal Linux builder; only these ordinary tests must run on scratch.
    control()
    prepare_tools()
    for arm in report["expected_arms"]:
        if arm=="stale" and report["arms"]["fixed"]["status"]!="PASS":
            report["arms"][arm]={"status":"INCOMPLETE","failure":"positive control did not pass; stale check cannot establish a regression","tests":{},"spawns":[]}
            break
        if arm == "stale":
            with Path("CODEOWNERS").open("a") as fp:
                fp.write("\n" + stale + "\n")
        try:
            record(arm)
        except Exception as exc:
            report["arms"][arm]["failure"] = str(exc)[:1900]
            print(arm,"incomplete or unexpected:",str(exc)[:720],flush=True)
        finally:
            report["arms"][arm]["verified_at"] = now()
            save()
    report["verdict"] = "PASS" if all(r["status"]=="PASS" for r in report["arms"].values()) else "FAIL"
except Exception as error:
    report["failure"]=str(error)[:1600]
    report["verdict"]="INCOMPLETE"
finally:
    report["verified_at"] = now()
    save()
sys.exit(report.get("verdict") != "PASS")
