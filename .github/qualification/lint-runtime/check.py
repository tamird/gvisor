"""Verify the configured remote Buildifier and cpplint scratch actions."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SHA = "a4eb4f3835bfb8a80b2dec236d17a75aa5e01334"
TREE = "9fb1442f1cea3b60c7e1d912c6fe6078dca5fea8"
GNU_IMAGE = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
PYTHON = "02939080d36c0fd2dc2507ff146a4c86360fa3968f30e1e42a6424d352629394"
CPP = "f3920e47d89eba739f866405745ce6b88db1395b8dacf83d880bac95043fa8d5"
BUILDIFIER = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
ROOT = Path(os.environ["RUNNER_TEMP"])
CASE = Path("test/util/.lint_pilot_control")
BAD_BZL = str(CASE / "bad formatting.bzl")
BAD_CPP = str(CASE / ".new file.cc")
CFG = str(CASE / "CPPLINT.cfg")
OUT = {"source": SHA, "tree": TREE, "verdict": "INCOMPLETE", "phases": {}}


def check(ok, msg):
    if not ok:
        raise ValueError(msg)


def save():
    (ROOT / "native-lint-runtime.json").write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(s):
    return sha256(s).hexdigest()


def rows(path):
    if not path.is_file():
        return
    s = path.read_text()
    d = json.JSONDecoder()
    i = 0
    while i < len(s):
        while i < len(s) and s[i].isspace():
            i += 1
        if i == len(s):
            return
        row, i = d.raw_decode(s, i)
        yield row


def git(*args):
    return subprocess.check_output(["git", *args])


def names(paths, tool):
    if tool == "Cpplint":
        return {p for p in paths if p.endswith((".c", ".cc", ".h"))}, {p for p in paths if Path(p).name == "CPPLINT.cfg"}
    return {p for p in paths if Path(p).name in {"BUILD", "BUILD.bazel"} or p.endswith(".bzl") or p in {"MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"}}, {p for p in paths if p == ".buildifier.json"}


def report(phase, tool, spawn, wanted, configs, wanted_exit, log):
    args = spawn["commandArgs"]
    fs = {f["path"]: f for f in spawn.get("inputs", [])}
    props = {p["name"]: p["value"] for p in spawn.get("platform", {}).get("properties", [])}
    rows = [{"name": k, "digest": v["digest"]} for k, v in fs.items() if "/ROOT.d/" in k]
    actual = {r["name"].split("/ROOT.d/", 1)[1]: r["digest"]["hash"] for r in rows}
    wanted_hash = {p: digest(Path(p).read_bytes()) for p in sorted(wanted | configs)}
    sep = args.index("--")
    native = args[sep + 1]
    row = {"owner": spawn.get("targetLabel"), "runner": spawn.get("runner"), "cache": spawn.get("cacheHit", False),
           "action_exit": spawn.get("exitCode", 0), "worker_image": props.get("container-image"), "worker_arch": props.get("Arch"),
           "source_count": len(wanted), "source_and_config_count": len(actual), "config": sorted(configs),
           "membership_sha256": digest("\0".join(sorted(actual)).encode()), "source_bytes_sha256": digest(json.dumps(actual, sort_keys=True).encode()),
           "inputs_match": actual == wanted_hash, "spawn": fs[args[0]]["digest"], "program": fs[native]["digest"],
           "output": [f.get("digest") for f in spawn.get("actualOutputs", []) if f["path"].endswith(".report")]}
    phase["actions"][tool] = row
    row["program_path"] = native
    check(row["runner"] == "remote" and not row["cache"] and row["worker_image"] == phase["image"] and row["worker_arch"] == "amd64", "wrong physical execution")
    check(row["inputs_match"] and len(rows) == len(wanted_hash), "incomplete declared source bytes")
    check(row["action_exit"] == wanted_exit, "unexpected native checker exit")
    direct = [a.split("/ROOT.d/",1)[1] for a in args if "/ROOT.d/" in a and not a.startswith("--")]
    check(direct == sorted(wanted), "argument dropped, added or split a source path")
    if tool == "BuildifierCheck":
        check(row["program"]["hash"] == BUILDIFIER and "--mode=check" in args and "--format=" in args and "--config=off" in args, "wrong native Buildifier or ambient config")
        row["diagnostic"] = BAD_BZL + " # reformat" in log
    else:
        check(args[sep+2] == "-IB", "wrong Python flags")
        if phase["image"] == IMAGE:
            check(row["program"]["hash"] == PYTHON, "registered static interpreter differs from the pinned runtime")
        else:
            check(row["program"]["hash"] != PYTHON, "default unexpectedly selected static Python")
        row["cpplint_main"] = fs[args[sep+3]]["digest"]
        check(row["cpplint_main"]["hash"] == CPP, "wrong cpplint main")
        row["diagnostic"] = bool(re.search(re.escape(BAD_CPP)+r":3:.*\[build/include_order\]", log))
    check(row["diagnostic"] == bool(wanted_exit), "wrong controlled checker diagnostic")
    if not wanted_exit:
        check(len(row["output"]) == 1 and bool(re.fullmatch(r"[0-9a-f]{64}", row["output"][0]["hash"])), "missing successful report")


def build(name, paths, exits):
    d = ROOT / ("pilot-" + name)
    d.mkdir()
    static = name != "normal-gnu"
    image = IMAGE if static else GNU_IMAGE
    r = {"start": utc(), "actions": {}, "errors": [], "image": image}
    OUT["phases"][name] = r
    p = subprocess.run(["bazel", "--bazelrc="+os.environ["QA_RC"], "build", "--keep_going",
        *(["--config=static-python"] if static else []), "--config=x86_64",
        "--remote_default_exec_properties=OSFamily=linux", "--remote_default_exec_properties=Arch=amd64",
        "--remote_default_exec_properties=container-image="+image, "--remote_default_exec_properties=workload-isolation-type="+("oci" if static else "firecracker"),
        "--remote_default_exec_properties=EstimatedMemory=2GB",
        "--action_env=BUILDIFIER_CONFIG=example", "--noremote_accept_cached",
        "--build_event_json_file="+str(d/"events"), "--execution_log_json_file="+str(d/"exec"),
        "//tools/lint:check"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    msg = p.stdout.decode(errors="replace")
    r.update(finish=utc(), bazel_exit=p.returncode, log_sha256=digest(p.stdout))
    r["short_errors"] = [line[:300] for line in msg.splitlines() if any(k in line.lower() for k in ("error:", "no such file", "traceback", "failed:", "site.py", "no matching")) and not any(k in line.lower() for k in ("header", "api-key", "token", "authorization", "credential"))][-8:]
    # Only publish short, known setup categories and identifiers, not raw logs.
    r["categories"] = [c for c in ("no such package", "no such target", "does not contain symbol", "Error in fail", "no matching toolchains","not a valid executable", "Visibility error", "cycle in dependency graph", "Traceback", "already exists", "incorrect name", "has no attribute", "No such file or directory", "expected value", "is not visible", "name is not defined") if c.lower() in msg.lower()]
    r["source_failure_lines"] = sorted(set(re.findall(r"(?:(?:tools/lint_sources|tools/lint/defs)[.]bzl|tools/lint/BUILD|MODULE[.]bazel):[0-9]+:[0-9]+",msg)))[:20]
    for fixed in ("temporarily immutable due to active for-loop iteration", "no such repository", "Unrecognized option", "downloads are disabled", "does not have mandatory providers", "non-singleton filegroup", "NameError", "cannot be expanded", "is not executable", "expected type", "method 'get_child'", "No module named", "Failed to build necessary tools", "Error in bzl_harness_test", "unsupported format character", "is not a declared prerequisite", "cannot create implicit source file target"):
        if fixed.lower() in msg.lower():r["categories"].append(fixed)
    events = list(rows(d/"events"))
    r["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
    r["finished"] = [e["finished"].get("exitCode") for e in events if "finished" in e]
    r["native_failure_codes"] = sorted({k+":"+str(v.get("code")) for e in events for k,v in e.get("action",{}).get("failureDetail",{}).items() if isinstance(v,dict)})
    spawns = list(rows(d/"exec"))
    for t in ["BuildifierCheck", "Cpplint"]:
        observed = [s for s in spawns if s.get("mnemonic") == t]
        if len(observed) != 1:
            r["errors"].append(t+": no unique action ("+str(len(observed))+")")
            continue
        try:
            srcs,configs=names(paths,t)
            report(r,t,observed[0],srcs,configs,exits.get(t,0),msg)
        except (ValueError,KeyError,IndexError,OSError) as exc:
            r["errors"].append(t+":"+ (str(exc) if isinstance(exc,ValueError) else type(exc).__name__))
    if p.returncode != int(bool(exits)) or not r["invocation"] or len(r["finished"]) != 1:
        r["errors"].append("unexpected aggregate Bazel exit or missing BEP")
    r["verified"] = not r["errors"]
    save()
    print(name,r["invocation"],p.returncode,r["categories"],r["errors"],flush=True)
    return bool(r["actions"])


def main():
    check(git("rev-parse","HEAD","HEAD^{tree}").decode().splitlines()==[SHA,TREE], "wrong product source")
    tracked = {p.decode() for p in git("ls-files","-z").split(b"\0") if p}
    check(len(names(tracked,"Cpplint")[0])==485,"wrong C++ baseline")
    OUT["expected"] = ["clean","new-package","external"]
    OUT["requested_target"] = "//tools/lint:check"
    OUT["local_config_proof"] = "https://app.buildbuddy.io/invocation/b2d6486d-fde4-48b3-9a46-36b864ae9271"
    build("clean", tracked, {})
    CASE.mkdir()
    (CASE/"BUILD.bazel").write_text("# New package; no filegroup or orphan target.\n")
    Path(BAD_CPP).write_text("// New source, not in the Git index.\n#include <vector>\n#include <stdio.h>\n")
    build("new-package", tracked | {str(CASE/"BUILD.bazel"), BAD_CPP}, {"Cpplint":1})
    Path(BAD_CPP).unlink()
    (CASE / "BUILD.bazel").unlink()
    CASE.rmdir()
    from consumer import verify
    OUT["external"] = verify()
    success = all(v["verified"] for v in OUT["phases"].values()) and OUT["external"]["characterized"]
    OUT["checks_finished_at"] = utc()
    OUT["verdict"] = ("PASS" if OUT["external"]["compiled"] else "PREEXISTING_DOWNSTREAM_BLOCK") if success else "FAIL"
    save()
    return int(not success)


if __name__ == "__main__":
    save()
    try:
        sys.exit(main())
    except (OSError,ValueError,KeyError,IndexError) as exc:
        OUT["verdict"]="FAIL"
        OUT["collector_error"]=type(exc).__name__
        save()
        raise
