"""Run the existing write_source_files test suite on an empty execution worker."""
from datetime import datetime, timezone
import hashlib
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

def record(arm):
    result = report["arms"][arm] = {"status": "INCOMPLETE", "started_at": now(), "tests": {}, "spawns": []}
    save()
    bep, spawns = temp / f"{arm}.bep", temp / f"{arm}.compact"
    args = [str(temp / "native-bazel"), "--bazelrc=" + str(temp / "governance.bazelrc"), "test", "--jobs=45",
            "--keep_going", "--config=x86_64", "--noincompatible_exclude_starlark_flags_from_exec_config",
            "--incompatible_use_native_posix_test_wrapper", "--features=fully_static_link", "--host_features=fully_static_link",
            "--workspace_status_command=", "--lockfile_mode=update", "--test_output=errors", "--nocache_test_results",
            "--host_platform=@governance_workers//:amd64", "--extra_execution_platforms=@governance_workers//:amd64",
            "--remote_download_outputs=toplevel", "--execution_log_compact_file=" + str(spawns),
            "--build_event_json_file=" + str(bep), "--nobuild_event_json_file_path_conversion"]
    if arm != "stock":
        args.append("--override_module=bazel_lib=" + str(supplier))
    r = subprocess.run([*args, target], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    result.update(exit=r.returncode, execution_finished_at=now())
    logs = sanitise(r.stdout)
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
    for label, t in result["tests"].items():
        log = Path("bazel-testlogs") / label.split(":")[1] / "test.log"
        if log.is_file():
            t["log"] = sanitise(log.read_text(errors="replace"))[-20:]
    save()
    print(arm, result["exit"], result.get("invocation"), len(result["spawns"]), json.dumps(result["tests"]), flush=True)
    require(set(result["tests"]) == set(labels), result["diagnostics"])
    require(len(result["spawns"]) == 2 and {s["label"] for s in result["spawns"]} == set(labels), result["spawns"])
    require(all(s["image"] == image and s["arch"] == "amd64" and s["runner"] == "remote" and not s["cached"]
                and "compiled_posix_test_wrapper" in s["argv"][0] for s in result["spawns"]), result["spawns"])
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
    edit("BUILD", '    name = "governance-regen",', '    name = "governance-regen",\n    exec_properties = {"container-image": "'+image+'"},\n    tags = ["no-remote-cache"],')
    # All other actions run on a normal Linux builder; only these ordinary tests must run on scratch.
    control()
    for arm in report["expected_arms"]:
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
finally:
    report["verified_at"] = now()
    save()
sys.exit(report.get("verdict") != "PASS")
