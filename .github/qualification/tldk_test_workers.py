"""Run the public static backtrace test and inspect its actual test configuration."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import traceback
from urllib.parse import unquote, urlsplit

import native_cross_inputs as evidence
import tldk_installed as setup
import tldk_musl as previous
import tldk_public_inputs as inputs

ROOT = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
CLIENT = str(ROOT / "bazel")
SOURCE = "8aaa1cb430c30c4e69ef06f8e1a2252d28178925"
STOCK = "b2c944f33f8e85be86e3ae9c95f3a141ea57cbc2"
MIXED = "55e7265abbd508e773fa259c1ddd6e3182d5296e"
TEST = previous.TEST
ARCHIVE = previous.ARCHIVE
FLAGS = setup.FLAGS
FILES = [".bazelrc", "external/tools/plugin-stack/BUILD",
         "external/tools/plugin-stack/plugin-stack.BUILD", "external/tools/plugin-stack/toolchain.bzl"]
REPORT = {"product": SOURCE, "mixed": MIXED, "carrier": os.environ["CARRIER"],
          "cells": {}, "verified": False}


def save():
    (ROOT / "workers.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def invoke(args, timeout=450):
    rc, out, err = evidence.command(args, timeout)
    if rc:
        raise RuntimeError((Path(args[0]).name, rc, evidence.diagnostics(err + b"\n" + out)["lines"][-7:]))
    return out


def shutdown():
    invoke([CLIENT, "shutdown"], 120)


def graph(row):
    # This reads precisely the preceding test command's configured actions.
    # A separate ordinary aquery would not inherit test-only rc options.
    raw = invoke([CLIENT, "aquery", "--skyframe_state", "--noinclude_artifacts",
                  "--noinclude_commandline", "--output=jsonproto"], 480)
    graph = json.loads(raw)
    cfgs = {str(x["id"]): x for x in graph["configuration"]}
    owners = {str(x["id"]): previous.label(x["label"]) for x in graph["targets"]}
    actions = []
    for action in graph["actions"]:
        key = owners.get(str(action.get("targetId"))), action["mnemonic"]
        if key not in {(TEST, "TestRunner"), (TEST, "GoLink"), (ARCHIVE, "CcMakeMakeRule")}:
            continue
        cfg = cfgs[str(action["configurationId"])]
        actions.append({"owner": key[0], "action": key[1], "cfg": cfg["checksum"],
                        "exec": action.get("executionPlatform"), "target_cpu": cfg["platformName"]})
    row["actions"] = actions
    assert {(a["owner"], a["action"]) for a in actions} == {
        (TEST, "TestRunner"), (TEST, "GoLink"), (ARCHIVE, "CcMakeMakeRule")}, actions
    assert all(a["exec"] for a in actions)
    checksum, = {a["cfg"] for a in actions if a["action"] == "TestRunner"}
    cfg = json.loads(invoke([CLIENT, "config", "--output=json", checksum]))
    assert cfg["configHash"] == checksum
    row["test_options"] = {key: value for section in cfg["fragmentOptions"]
                           for key, value in section["options"].items()
                           if key in {"platforms", "host_platform", "extra_execution_platforms"}}
    return actions


def original_xml(bep, attempt):
    for line in bep.open():
        event = json.loads(line)
        ident = event["id"].get("testResult", {})
        if previous.label(ident.get("label", "")) != TEST:
            continue
        record, = [f for f in event["testResult"]["testActionOutput"] if f["name"] == "test.xml"]
        uri = urlsplit(record["uri"])
        assert uri.scheme == "file" and not uri.netloc
        raw = Path(unquote(uri.path)).read_bytes()
        assert (len(raw), hashlib.sha256(raw).hexdigest()) == (attempt["xml"]["bytes"], attempt["xml"]["sha256"])
        (ROOT / "test.xml").write_bytes(raw)
        return
    raise ValueError("missing original XML")


def native(row):
    git("checkout", "-q", "--detach", SOURCE)
    assert git("rev-parse", "HEAD^{tree}") == "b212939776a9ce0c0bccc00fdb41e31559c1e3d1"
    assert "$(call test,--config plugin-tldk @libpluginstack//:backtrace_test)" in Path("Makefile").read_text()
    setup.build_inputs("backtrace", ["--config=plugin-tldk", "--strip=never"], [TEST])
    shutdown()
    opts = [*FLAGS, "--strategy=TestRunner=local", "--nocache_test_results",
            "--flaky_test_attempts=1", "--runs_per_test=1", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--test_filter=^TestDumpStack$", "--test_arg=-test.v", "--nobuild_event_json_file_path_conversion"]
    rc, _, err = evidence.command(["make", "test", "DOCKER_BUILD=false", "OPTIONS=--config=plugin-tldk",
                                   "TARGETS=" + TEST, "BAZEL_OPTIONS=" + " ".join(opts)], 2800)
    row["make_exit"] = rc
    bep = Path(".build_events.json")
    previous.read_events(bep, row)
    if rc:
        raise RuntimeError(("make test", rc, evidence.diagnostics(err)["lines"][-7:]))
    attempt, = row["attempts"]
    assert attempt["owner"] == TEST and attempt["id"] == [1, 1, 1] and not attempt["cached"]
    assert attempt["status"] == "PASSED" and attempt["strategy"] in {"local","standalone"}
    assert attempt["xml"]["cases"] == [["TestDumpStack","PASS"]]
    assert any("--- PASS: TestDumpStack " in line for line in attempt["log"]["case"])
    assert row["targets"][TEST] == {"cfg": attempt["cfg"], "ok": True}
    original_xml(bep, attempt)
    actions = graph(row)
    assert {a["cfg"] for a in actions if a["owner"] == TEST} == {attempt["cfg"]}
    # Bazel's built-in default is an alias to @platforms//host:host.
    assert all(a["exec"].endswith(("//host:host", "//tools:host_platform")) for a in actions), actions
    assert "linux_x86_64_musl" in row["test_options"]["platforms"]
    events = setup.consumer.events(bep)
    owner, = [k for e in events if (k := e["id"].get("targetCompleted"))
              and previous.label(k["label"]) == TEST and not k.get("aspect")]
    assert owner["configuration"]["id"] == attempt["cfg"]
    out, = setup.consumer.outputs(events, owner["label"]).values()
    row["elf"] = previous.physical(out)
    row["elf"]["configuration"] = attempt["cfg"]
    assert row["elf"]["machine"] == 62 and not row["elf"]["interpreter"] and not row["elf"]["needed"]


def set_mixed(fixed):
    git("checkout", "-q", "--detach", "--force", os.environ["CARRIER"])
    git("restore", "--source=" + MIXED, "--staged", "--worktree", ".github/workflows/build.yml")
    git("rm", "-q", ".github/qualification/tldk_test_workers.py")
    if not fixed:
        delta = subprocess.check_output(["git", "diff", STOCK, SOURCE, "--", *FILES])
        subprocess.run(["git", "apply", "-R", "--index"], input=delta, check=True)
    expected = ("d4a2eac9c5c9b677e6a1585f691118a867714670" if fixed
                else "43d06930321b5b4ac646d33410a2d53ef2a31b0a")
    assert git("write-tree") == expected and not git("diff", "--name-only")
    return expected


def route(row, fixed, plugin_last):
    shutdown()
    row["tree"] = set_mixed(fixed)
    configs = ["--config=source-presubmit-mixed", "--config=plugin-tldk"]
    if not plugin_last:
        configs.reverse()
    row["configs"] = configs
    pattern = ROOT / "target.txt"
    pattern.write_text(TEST + "\n")
    # Limit the mixed profile to its one named owner. The analysis has no
    # endpoint and executes no actions; local strategies permit it offline.
    # Both profiles set --platforms. Hold the test binary's target constant
    # when measuring the effect of flag order on the execution platforms.
    target = "--platforms=@llvm//platforms:linux_x86_64_musl"
    offline = ["--spawn_strategy=local", "--remote_cache=", "--remote_executor=", "--bes_backend="]
    args = [CLIENT, "test", *configs, target, *offline,
            "--target_pattern_file=" + str(pattern), "--strategy=TestRunner=local",
            "--lockfile_mode=off", "--nobuild", "--jobs=4"]
    distdirs = [f for f in FLAGS if f.startswith("--distdir=")]
    rc, _, err = evidence.command([*args, *distdirs], 1200)
    if rc and b"Error downloading" in err:
        # The real mixed module upgrades rules_go. Resolve any reported
        # archives with the same checksum-bound public transport as the build.
        row["initial_fetch"] = evidence.diagnostics(err)
        setup.build_inputs("mixed_public_inputs", [*configs, target, *offline, "--nobuild"], [TEST])
        shutdown()
        invoke([*args, *[f for f in FLAGS if f.startswith("--distdir=")]], 1200)
    elif rc:
        raise RuntimeError(("test --nobuild", rc, evidence.diagnostics(err)["lines"][-7:]))
    actions = graph(row)
    assert "linux_x86_64_musl" in row["test_options"]["platforms"], row["test_options"]
    for a in actions:
        if fixed:
            assert a["exec"].removeprefix("@@") == "//test/rbe:scratch_build_amd64", actions
        else:
            assert "scratch_build_" not in a["exec"], actions
    row["mode"] = "configured only"


def older_client():
    client = ROOT / "bazel8"
    if not client.is_file():
        return {"observed": False, "reason": "verified Bazel 8.8.1 client unavailable"}
    shutdown()
    git("checkout", "-q", "--detach", "--force", SOURCE)
    # Report partial loading evidence. Dependency failures before the affected
    # rule was evaluated cannot establish an older version boundary.
    args = [str(client), "--ignore_all_rc_files", "query", "--keep_going", "--lockfile_mode=off",
            *[f for f in FLAGS if f.startswith("--distdir=")],
            "set(//:license @libpluginstack//:backtrace_test)"]
    rc, out, err = evidence.command(args, 180)
    lines = err.decode(errors="replace").splitlines()
    return {"client": "8.8.1", "exit": rc, "labels": out.decode(errors="replace").splitlines()[:4],
            "exec_group_diagnostics": [line.strip()[:450] for line in lines if "exec group" in line.lower()
                                      or "execution group" in line.lower()][:3],
            "diagnostics": evidence.diagnostics(err)["lines"][-5:]}


def cell(name, fn, *args):
    row = REPORT["cells"][name] = {}
    try:
        fn(row, *args)
        row["verified"] = True
    except Exception as error:
        row["error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno,
                        "detail": str(error)[:1700]}
    save()


def main():
    assert os.uname().machine == "x86_64" and os.getuid() != 0
    seeds = json.loads((ROOT / "tldk_installed_inputs.json").read_text())
    setup.R.clear()
    setup.R.update(source=SOURCE, carrier=os.environ["CARRIER"], preparation={}, receipts={},
                   scope="dependency and build preparation only")
    for directory, rec in inputs.seed(seeds):
        if directory:
            FLAGS.append("--distdir=" + str(directory))
            setup.R["receipts"][rec["source"]] = rec
    cell("native_make", native)
    cell("stock_mixed_then_plugin", route, False, True)
    cell("fixed_mixed_then_plugin", route, True, True)
    cell("fixed_plugin_then_mixed", route, True, False)
    REPORT["verified"] = all(v.get("verified") for v in REPORT["cells"].values())
    save()
    try:
        REPORT["older_client_load"] = older_client()
    except Exception as error:
        REPORT["older_client_load"] = {"observed": False, "reason": str(error)[:500]}
    save()
    return not REPORT["verified"]


if __name__ == "__main__":
    raise SystemExit(main())
