"""Exercise gVisor's check and local fix command on the declared scratch executor."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

WORK = Path.cwd()
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "native-lint-pilot.json"
B = TMP / "lint-bazel9"
SUPPLIER = "0d4bbc198943e150cd0f1c8f27140d498aba67f3"
SPAWN = "c66dec06777daf38a73278eb578c726b118ac3a93d5ae68f15cd132cc2b0723d"
TOOLS = {
    "gvisor-8.5.1": "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7",
    "supplier-10.1.0": "31b6a8aa1e5c746696788f428729701770ad91925873d8256cb885c60e12c77e",
}
result = {"verdict": "INCOMPLETE", "phases": {}}


def save():
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def verify(test, message):
    if not test:
        raise ValueError(message)


def digest(data):
    return sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=WORK)


def read_json(path):
    if not path.is_file():
        return []
    decoder = json.JSONDecoder()
    source = path.read_text()
    i = 0
    records = []
    while i < len(source):
        if source[i].isspace():
            i += 1
            continue
        entry, i = decoder.raw_decode(source, i)
        records.append(entry)
    return records


def src(path):
    return (Path(path).name in {"BUILD", "BUILD.bazel"} or path.endswith(".bzl")
            or path in {"MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"})


def command(arm, phase, verb, target, cwd):
    name = arm + "-" + phase
    target_dir = TMP / name
    target_dir.mkdir()
    events = target_dir / "bep.json"
    execution = target_dir / "exec.json"
    argv = [str(B / "bazel"), "--bazelrc=" + os.environ["CPPLINT_RC"], verb,
            "--config=x86_64", "--noshow_progress", "--noremote_accept_cached",
            "--extra_execution_platforms=@tcp_gnu//:scratch", "--host_platform=@tcp_gnu//:scratch",
            "--build_event_json_file=" + str(events), "--execution_log_json_file=" + str(execution)]
    if arm == "supplier-10.1.0":
        argv.append("--override_module=buildifier_prebuilt=" + str(B / "supplier"))
    proc = subprocess.run([*argv, target], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = proc.stdout.decode(errors="replace")
    beps = read_json(events)
    spawns = read_json(execution)
    info = {
        "bazel_exit": proc.returncode,
        "invocation": next((e["started"]["uuid"] for e in beps if "started" in e), None),
        "mnemonics": sorted({s["mnemonic"] for s in spawns if "mnemonic" in s}),
        "log_sha256": digest(proc.stdout),
        "first_errors": [line[:750] for line in log.splitlines()
                         if ("ERROR:" in line or "failed to fetch" in line.lower() or
                             "no such file" in line.lower()) and
                         not any(s in line.lower() for s in ("header", "authorization", "credential", "secret", "token=", "env -"))][:5],
    }
    result["phases"][name] = info
    save()
    print(name, info["invocation"], proc.returncode, info["mnemonics"], flush=True)
    verify(info["invocation"], "missing Bazel invocation")
    return info, log, spawns


def lint(arm, phase, paths):
    wanted = {p: digest((WORK / p).read_bytes()) for p in paths}
    info, log, spawns = command(arm, phase, "build", "//tools/lint:buildifier", WORK)
    actions = [s for s in spawns if s.get("mnemonic") == "BuildifierCheck"]
    verify(len(actions) == 1, "expected one physical check action")
    action = actions[0]
    props = {p["name"]: p["value"] for p in action.get("platform", {}).get("properties", [])}
    args = action["commandArgs"]
    fs = {f["path"]: f["digest"]["hash"] for f in action["inputs"]}
    tool = args[args.index("--") + 1]
    scanned = {p.split("/ROOT.d/", 1)[1]: h for p, h in fs.items() if "/ROOT.d/" in p}
    paths_in_args = [p.split("/ROOT.d/", 1)[1] for p in args if "/ROOT.d/" in p and not p.startswith("--")]
    diagnostics = sorted(p for p in wanted if "/ROOT.d/" + p + " # reformat" in log
                         or "/ROOT.d/" + p + ":" in log)
    info.update({"runner": action.get("runner"), "cache_hit": action.get("cacheHit", False),
                 "action_exit": action.get("exitCode", 0), "worker_image": props.get("container-image"),
                 "worker_arch": props.get("Arch"), "tool": tool, "tool_sha256": fs[tool],
                 "launcher": args[0], "launcher_sha256": fs[args[0]],
                 "declared_sources": len(scanned), "expected_sources": len(wanted),
                 "membership_sha256": digest("\0".join(sorted(scanned)).encode()),
                 "source_bytes_sha256": digest(json.dumps(scanned, sort_keys=True).encode()),
                 "matched_source_bytes": scanned == wanted, "matched_argv": paths_in_args == sorted(paths),
                 "contains_client_path": any(str(WORK) in x for x in [*args, *fs]),
                 "original_diagnostics": diagnostics})
    save()
    verify(info["runner"] == "remote" and not info["cache_hit"] and
           info["worker_image"] == "docker://" + os.environ["SCRATCH"] and info["worker_arch"] == "amd64",
           "wrong physical executor")
    verify(info["matched_source_bytes"] and info["matched_argv"] and not info["contains_client_path"],
           "input bytes, argv or remote path mismatch")
    verify(info["tool_sha256"] == TOOLS[arm] and info["launcher_sha256"] == SPAWN,
           "launched a different executable")
    verify("--mode=check" in args and "--config=off" in args and "--lint=warn" in args,
           "unexpected Buildifier check mode")
    verify(info["bazel_exit"] == int(info["action_exit"] != 0), "Bazel and checker disagree")
    return info


def run_formatter(arm, phase, cwd):
    info, _, _ = command(arm, phase, "run", "//tools/lint:format", cwd)
    verify(info["bazel_exit"] == 0, "formatter failed")
    data = (WORK / "bazel-bin/tools/lint/format").read_bytes()
    info.update({"client_binary_sha256": digest(data), "client_machine": int.from_bytes(data[18:20], "little"),
                 "is_elf": data.startswith(b"\x7fELF")})
    save()
    verify(info["client_binary_sha256"] == TOOLS[arm] and info["client_machine"] == 62 and info["is_elf"],
           "did not run the vendor Linux AMD64 formatter")


def stage_supplier():
    tar = B / "supplier.tar.gz"
    url = "https://codeload.github.com/tamird/buildifier-prebuilt/tar.gz/" + SUPPLIER
    subprocess.run(["curl", "--retry", "3", "--max-time", "120", "-fsSL", url, "-o", str(tar)], check=True)
    verify(digest(tar.read_bytes()) == result["supplier_archive_sha256"], "supplier archive hash mismatch")
    module = B / "supplier"
    module.mkdir()
    subprocess.run(["tar", "-xzf", str(tar), "--strip-components=1", "-C", str(module)], check=True)
    verify((module / "MODULE.bazel").is_file(), "supplier module missing")


def run_arm(arm, tracked):
    initial = lint(arm, "clean", tracked)
    if arm == "gvisor-8.5.1":
        verify(initial["action_exit"] == 0, "original formatter fails on clean master")
    else:
        verify(initial["action_exit"] in (0, 4), "supplier formatter failed before checking sources")
    root = WORK / "test/util/.lint_registered_tools"
    package = root / "ordinary"
    nested = root / "submodule"
    external = TMP / ("unstaged-" + arm)
    package.mkdir(parents=True)
    nested.mkdir()
    external.mkdir()
    (root / "generated").symlink_to(external, target_is_directory=True)
    untouched = b'X=    1\n'
    ignored = [external / "outside.bzl", nested / "ignored.bzl"]
    for p in ignored:
        p.write_bytes(untouched)
    (nested / "MODULE.bazel").write_text('module(name = "unrelated")\n')
    package_build = package / "BUILD"
    package_build.write_text('"""A package boundary inside the checkout."""\n')
    bad = package / "quoted $(literal) ' file.bzl"
    fixed = b'"""A same-server formatter control."""\n\nX = 1\n'
    bad.write_bytes(fixed.replace(b"X = 1", b"X=  1"))
    bp = str(bad.relative_to(WORK))
    with_fixture = tracked | {str(package_build.relative_to(WORK)), bp}
    try:
        damaged = lint(arm, "damaged", with_fixture)
        verify(damaged["action_exit"] == 4 and bp in damaged["original_diagnostics"],
               "malformed source did not produce its reformat diagnostic")
        run_formatter(arm, "format-from-subdirectory", package)
        verify(bad.read_bytes() == fixed and all(p.read_bytes() == untouched for p in ignored),
               "formatter failed to repair included file or touched excluded file")
        repaired = lint(arm, "repaired", with_fixture)
        verify(repaired["action_exit"] == 0, "repaired checkout still fails")
        changed = git("diff", "--name-only", "-z").decode().rstrip("\0").split("\0")
        repaired.update({"repaired_quoted_source": True, "ignored_nested_workspace_and_symlink": True,
                         "tracked_files_changed_by_fix": [p for p in changed if p]})
        save()
        if arm == "gvisor-8.5.1":
            verify(not any(changed), "original formatter modified already-checked sources")
    finally:
        shutil.rmtree(root)


def main():
    result["source"] = git("rev-parse", "HEAD").decode().strip()
    result["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    verify(result["source"] == "7b4a1a7b5a50e289d6b9eac9278d2c32836f1c99", "unexpected checkout")
    result["supplier"] = SUPPLIER
    result["supplier_archive_sha256"] = "ef30bc754957787640916a2cfd8246ed9d9223af0387150fd73a75530469fbb5"
    result["bazel"] = subprocess.check_output([str(B / "bazel"), "--version"]).decode().strip()
    verify(result["bazel"] == "bazel 9.3.1", "wrong Bazel")
    tracked = {p for p in git("ls-files", "-z").decode().split("\0") if src(p)}
    for arm in TOOLS:
        if arm == "supplier-10.1.0":
            stage_supplier()
        run_arm(arm, tracked)
    result["verdict"] = "PASS"
    save()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        result["error"] = str(e) if isinstance(e, ValueError) else type(e).__name__
        save()
        print("Qualification stopped:", result["error"], file=sys.stderr)
        sys.exit(1)
