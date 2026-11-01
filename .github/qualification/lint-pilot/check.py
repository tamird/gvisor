"""Verify the configured remote Buildifier and cpplint actions across same-server changes to the source tree."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SHA = "045a3f7a506c109eab8beb396e5b3af37776a2df"
TREE = "c7f01bd9cae56e5129c464c15583b9e7ec688da0"
IMAGE = "docker://" + os.environ["GNU"]
CPP = "f3920e47d89eba739f866405745ce6b88db1395b8dacf83d880bac95043fa8d5"
BUILDIFIER = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
ROOT = Path(os.environ["RUNNER_TEMP"])
BAZEL = [str(ROOT / "lint-bazel9" / "bazel"), "--bazelrc=" + os.environ["CPPLINT_RC"]]
CASE = Path("test/util/.lint_pilot_control")
BAD_BZL = str(CASE / "bad formatting.bzl")
BAD_CPP = str(CASE / ".new file.cc")
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
    row["launcher_path"] = args[0]
    row["program_path"] = native
    phase["actions"][tool] = row
    check(row["runner"] == "remote" and not row["cache"] and row["worker_image"] == IMAGE and row["worker_arch"] == "amd64", "wrong physical execution")
    check(row["spawn"]["hash"] == "fff56331defc1c191a62063c812cbc600fb42ca5824eda4fd41e601a804d36b4",
          "wrong native run_binary launcher")
    check(row["inputs_match"] and len(rows) == len(wanted_hash), "incomplete declared source bytes")
    check(row["action_exit"] == wanted_exit, "unexpected native checker exit")
    direct = [a.split("/ROOT.d/",1)[1] for a in args if "/ROOT.d/" in a and not a.startswith("--")]
    check(direct == sorted(wanted), "argument dropped, added or split a source path")
    if tool == "BuildifierCheck":
        check(row["program"]["hash"] == BUILDIFIER and "--mode=check" in args and "--format=" in args and "--config=off" in args, "wrong native Buildifier or ambient config")
        row["diagnostic"] = BAD_BZL + " # reformat" in log
    else:
        check(bool(re.fullmatch(r"python3(?:[.]\d+)?", Path(native).name)) and args[sep+2] == "-IB", "wrong native Python")
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
    p = subprocess.run([*BAZEL, "build", "--keep_going", "--config=x86_64",
        "--action_env=BUILDIFIER_CONFIG=example", "--noremote_accept_cached",
        "--build_event_json_file="+str(d/"events"), "--execution_log_json_file="+str(d/"exec"),
        "//tools/lint:check", "//:buildifier", "//:cpplint"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
    r["completed_targets"] = sorted(e["id"]["targetCompleted"]["label"] for e in events if e.get("completed", {}).get("success"))
    r["native_failure_codes"] = sorted({k+":"+str(v.get("code")) for e in events for k,v in e.get("action",{}).get("failureDetail",{}).items() if isinstance(v,dict)})
    if p.returncode:
        # Only category, source locations and our two intentional diagnostic lines leave the runner.
        r["bad_paths"] = {path: path in msg for path in (BAD_BZL, BAD_CPP)}
        for line in msg.splitlines():
            if re.search(r"ERROR:|^FATAL:", line) and not re.search(r"api.key|token|credential|remote.header|bes.header|://", line, re.I):
                print(line[:420], flush=True)
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


def inventory(name, paths):
    wanted = set.union(*(names(paths, tool)[0] for tool in ("BuildifierCheck", "Cpplint")))
    command = [*BAZEL, "cquery", "--config=x86_64", "--output=files", "set(@lint_inputs//:buildifier @lint_inputs//:cpplint)"]
    result = subprocess.run(command, capture_output=True, text=True)
    files = {line.split("/ROOT.d/", 1)[1] for line in result.stdout.splitlines() if "/ROOT.d/" in line}
    row = {"exit": result.returncode, "size": len(files), "expected": len(wanted),
           "missing": sorted(wanted-files)[:8], "extra": sorted(files-wanted)[:8],
           "fixture": sorted(p for p in files if p.startswith(str(CASE) + "/"))}
    row["verified"] = result.returncode == 0 and files == wanted
    OUT.setdefault("inventories", {})[name] = row
    save()


def server_pid():
    return int(subprocess.check_output([*BAZEL, "info", "server_pid"]).strip())


def main():
    global BAD_BZL, BAD_CPP
    check(git("rev-parse", "HEAD", "HEAD^{tree}").decode().splitlines() == [SHA, TREE], "wrong product source")
    check(subprocess.check_output([BAZEL[0], "--version"]).strip() == b"bazel 9.3.1", "wrong released Bazel")
    OUT["bazel"] = "9.3.1"
    OUT["targets"] = ["//tools/lint:check", "//:buildifier", "//:cpplint"]
    tracked = {p.decode() for p in git("ls-files", "-z").split(b"\0") if p}
    check(not CASE.exists(), "fixture already exists")
    OUT["initial_pid"] = server_pid()
    build("clean", tracked, {})
    for name in ("added", "recreated"):
        CASE.mkdir()
        BAD_BZL = str(CASE / (name + " bad formatting.bzl"))
        BAD_CPP = str(CASE / (name + " bad includes.cc"))
        files = {str(CASE / "BUILD.bazel"), BAD_BZL, BAD_CPP}
        try:
            (CASE / "BUILD.bazel").write_text("# Independent package.\n")
            Path(BAD_BZL).write_text("X= 1\n")
            Path(BAD_CPP).write_text("// A deliberately disordered include.\n#include <vector>\n#include <stdio.h>\n")
            inventory(name, tracked | files)
            build(name, tracked | files, {"BuildifierCheck": 4, "Cpplint": 1})
        finally:
            shutil.rmtree(CASE)
        inventory("removed-" + name, tracked)
    OUT["final_pid"] = server_pid()
    OUT["checks_finished_at"] = utc()
    ok = (OUT["initial_pid"] == OUT["final_pid"] and len(OUT["phases"]) == 3 and
          all(v["verified"] for v in OUT["phases"].values()) and
          len(OUT["inventories"]) == 4 and all(v["verified"] for v in OUT["inventories"].values()))
    OUT["verdict"] = "PASS" if ok else "INCOMPLETE"
    save()
    return int(not ok)


if __name__ == "__main__":
    save()
    try:
        sys.exit(main())
    except Exception as exc:
        OUT["collector_error"] = type(exc).__name__
        save()
        raise
