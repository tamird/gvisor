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

SHA = "07fd29494ed9b1a29fd81de936db93224bcddfe8"
TREE = "d00d63b53a50907d75e6ecd0b6acce2bb6cb5602"
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
    check(row["spawn"]["hash"] == "c66dec06777daf38a73278eb578c726b118ac3a93d5ae68f15cd132cc2b0723d",
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
    r["execution_finished"] = next((e["finished"].get("finishTime") for e in events if "finished" in e), None)
    if p.returncode:
        # Report only tracked lint input identities, never arbitrary diagnostics.
        tracked_inputs = set.union(*(names(paths, tool)[0] for tool in ("BuildifierCheck", "Cpplint")))
        r["lint_diagnostics"] = sorted({p for p in tracked_inputs if "/ROOT.d/" + p + " # reformat" in msg or "/ROOT.d/" + p + ":" in msg})[:50]
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
    check(git("rev-parse", "HEAD", "HEAD^{tree}").decode().splitlines() == [SHA, TREE], "wrong product source")
    check(subprocess.check_output([BAZEL[0], "--version"]).strip() == b"bazel 9.3.1", "wrong released Bazel")
    OUT["bazel"] = "9.3.1"
    OUT["targets"] = ["//tools/lint:check", "//:buildifier", "//:cpplint"]
    tracked = {p.decode() for p in git("ls-files", "-z").split(b"\0") if p}
    OUT["prior_source"] = "045a3f7a506c109eab8beb396e5b3af37776a2df"
    OUT["rebase_parent"] = git("rev-parse", "HEAD^").decode().strip()
    check(OUT["rebase_parent"] == "183a39d1b2aff9e8e2219171940001749a2b5db0", "wrong upstream base")
    gov = "6e1caeba747c9dcaa533bd9fe1a4232895ac04c2"
    OUT["dependency_source"] = gov
    OUT["overlay_sha256"] = {}
    # Replace only the two approved governance values on the clean product.
    for path, pattern, want in [
        ("MODULE.bazel", rb'^bazel_dep\(name = "bazel_lib".+$', "151f3b2327f019bf9575fab8e475646ae50038aad69c00bce554691b8dc728ab"),
        ("tools/licensecheck/dependencies.yaml", rb'^- dependency: bazel_lib\n(?:  [^\n]+\n)+', "b1c04baeb2bec7fd3158eaa49e62722db2054bcee04e99e17365178a66542ae5"),
    ]:
        rx = re.compile(pattern, re.M)
        base = Path(path).read_bytes()
        old, = rx.findall(base)
        selected, = rx.findall(git("show", gov + ":" + path))
        replaced = base.replace(old, selected)
        check(digest(replaced) == want, "wrong governance overlay")
        Path(path).write_bytes(replaced)
        OUT["overlay_sha256"][path] = want
    check(git("diff", "--name-only").decode().splitlines() == list(OUT["overlay_sha256"]), "unexpected source change")
    build("clean", tracked, {})
    check(not CASE.exists(), "negative fixture already exists")
    CASE.mkdir()
    try:
        Path(BAD_BZL).write_text("X= 1\n")
        Path(BAD_CPP).write_text("// Controlled disordered includes.\n#include <vector>\n#include <stdio.h>\n")
        build("malformed", tracked | {BAD_BZL, BAD_CPP}, {"BuildifierCheck": 4, "Cpplint": 1})
    finally:
        shutil.rmtree(CASE)
    ok = all(phase["verified"] for phase in OUT["phases"].values()) and OUT["phases"]["clean"]["completed_targets"] == sorted(OUT["targets"])
    OUT["checks_finished_at"] = utc()
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
