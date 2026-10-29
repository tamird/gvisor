"""Verify the configured remote Buildifier and cpplint scratch actions."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SHA = "5e53b735d4bb7c44280afbcc55a910711e88da0f"
TREE = "cab5b87ef4b0f432b08b71f5d1d2548e8e5f0440"
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
    (ROOT / "native-lint-pilot.json").write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")


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
    check(row["runner"] == "remote" and not row["cache"] and row["worker_image"] == IMAGE and row["worker_arch"] == "amd64", "wrong physical execution")
    check(row["inputs_match"] and len(rows) == len(wanted_hash), "incomplete declared source bytes")
    check(row["action_exit"] == wanted_exit, "unexpected native checker exit")
    direct = [a.split("/ROOT.d/",1)[1] for a in args if "/ROOT.d/" in a and not a.startswith("--")]
    check(direct == sorted(wanted), "argument dropped, added or split a source path")
    if tool == "BuildifierCheck":
        check(row["program"]["hash"] == BUILDIFIER and "--mode=check" in args and "--format=" in args and "--config=off" in args, "wrong native Buildifier or ambient config")
        row["diagnostic"] = BAD_BZL + " # reformat" in log
    else:
        check(row["program"]["hash"] == PYTHON and args[sep+2] == "-IB", "wrong native Python")
        row["cpplint_main"] = fs[args[sep+3]]["digest"]
        check(row["cpplint_main"]["hash"] == CPP, "wrong cpplint main")
        row["diagnostic"] = bool(re.search(re.escape(BAD_CPP)+r":3:.*\[build/include_order\]", log))
    check(row["diagnostic"] == bool(wanted_exit), "wrong controlled checker diagnostic")
    if not wanted_exit:
        check(len(row["output"]) == 1 and bool(re.fullmatch(r"[0-9a-f]{64}", row["output"][0]["hash"])), "missing successful report")


def build(name, paths, exits):
    d = ROOT / ("pilot-" + name)
    d.mkdir()
    r = {"start": utc(), "actions": {}, "errors": []}
    OUT["phases"][name] = r
    p = subprocess.run(["bazel", "--bazelrc="+os.environ["CPPLINT_RC"], "build", "--keep_going",
        "--host_platform=@cpplint_qualification//:scratch", "--extra_execution_platforms=@cpplint_qualification//:scratch",
        "--platforms=//tools/bazeldefs:linux_amd64", "--extra_toolchains=@cpplint_qualification//:static_python",
        "--action_env=BUILDIFIER_CONFIG=example", "--noremote_accept_cached",
        "--build_event_json_file="+str(d/"events"), "--execution_log_json_file="+str(d/"exec"),
        "//:buildifier", "//:cpplint"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    msg = p.stdout.decode(errors="replace")
    r.update(finish=utc(), bazel_exit=p.returncode, log_sha256=digest(p.stdout))
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
    q=subprocess.run(["bazel","--bazelrc="+os.environ["CPPLINT_RC"],"query","--ignore_dev_dependency","//:license"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    OUT["root_without_dev"]={"exit":q.returncode,"log_sha256":digest(q.stdout),"actual_target":"//:license" in q.stdout.decode(errors="replace").splitlines()}
    save()
    if not build("clean",tracked,{}):
        return 1
    CASE.mkdir()
    (CASE/"BUILD.bazel").write_text("# New package; no filegroup or orphan target.\n")
    Path(BAD_BZL).write_text("ITEMS= [1,2]\n")
    Path(BAD_CPP).write_text("// New source, not in the Git index.\n#include <vector>\n#include <stdio.h>\n")
    for sub,marker in (("vendored", ".git"), ("nested", "WORKSPACE.bazel")):
        dir=CASE/sub
        dir.mkdir()
        (dir/marker).touch()
        (dir/"ignored.bzl").write_text("INVALID=  [1,2]\n")
        (dir/"ignored.cc").write_text(Path(BAD_CPP).read_text())
    (CASE/"linked.cc").symlink_to(Path(BAD_CPP).absolute())
    paths=tracked|{str(CASE/"BUILD.bazel"),BAD_BZL,BAD_CPP}
    build("dirty",paths,{"BuildifierCheck":4,"Cpplint":1})
    Path(BAD_BZL).write_text("ITEMS = [1, 2]\n")
    Path(CFG).write_text("filter=-build/include_order\n")
    build("local-config",paths|{CFG},{})
    success=q.returncode==0 and OUT["root_without_dev"]["actual_target"] and all(p["verified"] for p in OUT["phases"].values())
    OUT["verdict"]="PASS" if success else "FAIL"
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
