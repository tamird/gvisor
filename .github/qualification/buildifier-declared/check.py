"""Verify the declared inputs, the actual lint action and its two public interfaces."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

BEFORE = "4089e4d00cf2cf178639b7ff6d83e0613e7814a0"
SOURCE = "2efeae631d80b7a8358cffa31552d902144b443e"
TREE = "5e5354ec2c8925491e4d4ce4f687ea3d2986ff3c"
TOOL = {"amd64": "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7", "arm64": "947bf6700d708026b2057b09bea09abbc3cafc15d9ecea35bb3885c4b09ccd04"}
EMPTY = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
CASE = Path("pkg/.buildifier-declared-new")
NEW = CASE / "a new.bzl"
NESTED = CASE / "nested/BUILD.bazel"
CONFIGURED = '{"format":"json","buildifier_disable":["listsort"]}\n'
UNSORTED = 'filegroup(\n    name = "nested",\n    srcs = [\n        "z.cc",\n        "a.cc",\n    ],\n)\n'
TEMP = Path(os.environ["RUNNER_TEMP"])
RAW = TEMP / "buildifier-raw"
RAW.mkdir()
RESULT = {"verdict": "INCOMPLETE", "source": SOURCE, "tree": TREE, "phases": []}
PATTERNS = ("BUILD", "*/BUILD", "BUILD.bazel", "*/BUILD.bazel", "*.bzl", "WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel")
SEVEN = {"MODULE.bazel", "governance/tools/maintainers/repositories.bzl", "tools/cc_compile_inputs.bzl", "tools/show_paths.bzl", *("tools/bazeldefs/extensions/" + n + ".bzl" for n in ("coral_crosstool", "crosstool", "llvm_zlib"))}


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return sha256(value).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args])


def records(path):
    data = path.read_text()
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(data):
        if data[pos].isspace():
            pos += 1
            continue
        obj, pos = decoder.raw_decode(data, pos)
        yield obj


def file_id(data):
    return {"sha256": sha(data), "bytes": len(data)}


def phase(name, arch, files, op="build", negative=None, fix_env=None, before=False):
    d = RAW / name
    d.mkdir()
    row = {"name": name, "arch": arch, "started": now()}
    RESULT["phases"].append(row)
    owner = {"build": "//:buildifier", "run": "//:buildifier_fix", "test": "//:buildifier_test"}[op]
    be, ex, log = (d / n for n in ("events.json", "exec.json", "combined.log"))
    plat = "@buildifier_platforms//:" + arch
    args = ["bazel", "--bazelrc=" + os.environ["PROOF_RC"], op, "--host_platform=" + plat, "--extra_execution_platforms=" + plat, "--platforms=//tools/bazeldefs:linux_" + arch, "--build_event_json_file=" + str(be), "--execution_log_json_file=" + str(ex), "--jobs=24"]
    if op == "test":
        args.append("--nocache_test_results")
    if fix_env is not None:
        args.append("--run_under=/usr/bin/env " + fix_env)
    with log.open("wb") as out:
        native = subprocess.run(args + [owner], stdout=out, stderr=subprocess.STDOUT, check=False)
    logdata = log.read_bytes()
    message = logdata.decode(errors="replace")
    row.update(finished=now(), exit=native.returncode, log=file_id(logdata), label=owner)
    events = list(records(be)) if be.exists() else []
    row["invocation"] = next((x["started"]["uuid"] for x in events if "started" in x), None)
    row["build_finish"] = [x["finished"].get("exitCode") for x in events if "finished" in x]
    row["diagnostic"] = [line for line in message.splitlines() if line.startswith(str(NEW)) and len(line) < 400]
    row["formatting"] = [m for m in message.splitlines() if re.fullmatch(r'[A-Za-z0-9_/ .+-]+ # reformat', m)]
    row["lint_locations"] = sorted({(m.group(1), m.group(2), m.group(3)) for line in message.splitlines()
                                    if (m := re.match(r"^(.+?):([0-9]+): ([A-Za-z0-9_-]+):", line)) and m.group(1) in files})
    errors = [x for x in message.splitlines() if x.startswith("ERROR:")]
    row["supplier_resolution_failed"] = "No matching toolchains found" in message and "buildifier" in message
    row["error_count"] = len(errors)
    row["error_digest"] = sha("\n".join(errors).encode())
    require(row["invocation"] and ex.exists(), "no invocation or execution record")
    if op == "run":
        require(native.returncode == 0, "explicit fix failed")
        row["run_environment"] = fix_env
        return
    lint = []
    tests = []
    for spawn in records(ex):
        if spawn.get("mnemonic") == "BuildifierCheck":
            lint.append(spawn)
        elif spawn.get("mnemonic") == "TestRunner":
            tests.append({
                "runner": spawn.get("runner"), "target": spawn.get("targetLabel"),
                "exit": spawn.get("exitCode", 0), "cached": spawn.get("cacheHit", False),
                "test_setup": any(a.endswith("/test-setup.sh") for a in spawn.get("commandArgs", [])),
                "xml": any("generate-xml" in a for a in spawn.get("commandArgs", [])),
            })
    require(len(lint) == 1, "no single BuildifierCheck action")
    act = lint[0]
    props = {p["name"]: p["value"] for p in act["platform"]["properties"]}
    row.update(runner=act.get("runner"), cache_hit=act.get("cacheHit", False), action_exit=act.get("exitCode", 0), platform={k: props.get(k) for k in ("Arch", "OSFamily", "container-image", "workload-isolation-type")})
    require(row["runner"] == "remote" and not row["cache_hit"] and props.get("Arch") == arch and props.get("container-image") == EMPTY and props.get("OSFamily") == "linux", "wrong worker, replay, or image")
    row["ambient_example_requested"] = props.get("env-overrides") == "BUILDIFIER_CONFIG=example"
    require(row["ambient_example_requested"], "missing injected non-secret sample setting")
    if before:
        row["example_emitted"] = all(v in message for v in ('"mode": "fix"', '"type": "auto"', '"warningsList":'))
        require(row["example_emitted"] and not row["formatting"], "predecessor did not print supplier example")
    argv = act["commandArgs"]
    require(len(argv) == 5 and argv[1] == "check", "wrong checker arguments")
    driver, _, tool, manifest, marker = argv
    physical = {x["path"]: x for x in act["inputs"]}
    require(physical[tool]["digest"]["hash"] == TOOL[arch], "wrong supplier binary")
    row["driver"], row["tool"], row["manifest"] = [physical[n]["digest"] for n in (driver, tool, manifest)]
    prefix = str(Path(manifest).parent) + "/src/"
    inputs = {n.removeprefix(prefix): f for n, f in physical.items() if n.startswith(prefix)}
    cfg = inputs.pop(".buildifier.json", None)
    if Path(".buildifier.json").is_file():
        config = file_id(Path(".buildifier.json").read_bytes())
        require(cfg and cfg["digest"]["hash"] == config["sha256"] and int(cfg["digest"]["sizeBytes"]) == config["bytes"], "missing declared configuration")
        row["configuration"] = config
    else:
        require(cfg is None, "unexpected configuration")
    require(set(inputs) == files, "wrong source names")
    facts = {}
    for name, value in inputs.items():
        data = Path(name).read_bytes()
        require(sha(data) == value["digest"]["hash"] and len(data) == int(value["digest"].get("sizeBytes", 0)), "physical source bytes mismatch: " + name)
        facts[name] = sha(data)
    manifest_value = sorted(files) if before else {"data": [".buildifier.json"] if cfg else [], "sources": sorted(files)}
    manifest_data = json.dumps(manifest_value, ensure_ascii=False, separators=(",", ":")).encode()
    require(physical[manifest]["digest"]["hash"] == sha(manifest_data), "manifest differs from source inputs")
    row["count"] = len(facts)
    row["source_membership"] = sha(("\0".join(sorted(files)) + "\0").encode())
    row["content_hashes"] = sha(json.dumps(facts, sort_keys=True).encode())
    row["previously_unexposed"] = {k: facts[k] for k in sorted(SEVEN)}
    row["vcs_input"] = any(".git" in Path(n).parts for n in physical)
    require(not row["vcs_input"], "VCS leaked into the worker")
    outputs = act.get("actualOutputs", [])
    row["outputs"] = outputs
    if negative:
        require(native.returncode != 0 and row["action_exit"] == 4 and len(row["formatting"]) == 1 and row["formatting"][0] == str(negative) + " # reformat" and not outputs, "wrong expected source rejection")
    else:
        require(native.returncode == 0 and row["action_exit"] == 0 and len(outputs) == 1 and outputs[0]["path"] == marker and outputs[0]["digest"]["hash"] == sha(b""), "clean lint did not pass")
    if op == "test":
        t = [x["testResult"] for x in events if x.get("id", {}).get("testResult", {}).get("label") == owner]
        require(len(t) == 1 and t[0]["status"] == "PASSED", "standard Bazel test didn't pass")
        row["test_result"] = t[0]["status"]
        row["test_actions"] = tests
        require(any(x["runner"] == "remote" and not x["cached"] and x["exit"] == 0 and x["target"] == owner and x["test_setup"] for x in tests), "missing physical standard test runner")


def inventory(files, name):
    target = "@starlark_sources//:inputs"
    args = ["bazel", "--bazelrc=" + os.environ["PROOF_RC"], "query", "--output=label", "labels(srcs, " + target + ")"]
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    row = {"phase": name, "exit": r.returncode, "stdout": file_id(r.stdout), "stderr": file_id(r.stderr)}
    RESULT.setdefault("one_package_queries", []).append(row)
    require(r.returncode == 0, "root projected filegroup does not resolve")
    paths = set()
    for line in r.stdout.decode().splitlines():
        parts = line.split("//:src/", 1)
        require(len(parts) == 2 and parts[0].startswith("@"), "input owned by another package")
        paths.add(parts[1])
    row["root_package_direct_source_count"] = len(paths)
    require(paths == files, "root filegroup input differs from source census")


def main():
    require(git("rev-parse", "HEAD").decode().strip() == SOURCE and git("rev-parse", "HEAD^{tree}").decode().strip() == TREE, "wrong product checkout")
    files = {p.decode() for p in git("ls-files", "-z", "--", *PATTERNS).split(b"\0") if p}
    require(len(files) == 617 and SEVEN <= files and not git("status", "--porcelain", "--untracked-files=no"), "wrong initial file set")
    root = Path.cwd()
    inventory(files, "candidate unchanged source set")
    os.chdir(root.parent / "before")
    require(git("rev-parse", "HEAD").decode().strip() == BEFORE, "incorrect original source")
    stock_files = {p.decode() for p in git("ls-files", "-z", "--", *PATTERNS).split(b"\0") if p}
    before_bad = CASE / "BUILD"
    before_bad.parent.mkdir()
    malformed = 'filegroup(name="nested",srcs=["z.cc","a.cc"])\n'
    before_bad.write_text(malformed)
    require(len(stock_files) == 617 and not Path(".buildifier.json").exists(), "unexpected predecessor inputs")
    phase("old-example-false-green", "amd64", stock_files | {str(before_bad)}, before=True)
    require(before_bad.read_text() == malformed, "old check changed the bad file")
    RESULT["original"] = {"source": BEFORE, "known_malformed_file":str(before_bad), "content":file_id(malformed.encode())}
    os.chdir(root)
    NESTED.parent.mkdir(parents=True)
    NESTED.write_text(malformed)
    for n in ("a.cc", "z.cc"):
        (NESTED.parent / n).touch()
    Path("WORKSPACE.bazel").write_text("# First-party legacy repository marker.\n")
    ignored = CASE / "external-checkout"
    ignored.mkdir()
    git("init", "--quiet", str(ignored))
    (ignored / "BUILD.bazel").write_text(malformed)
    for n in ("MODULE.bazel", "REPO.bazel", "WORKSPACE", "WORKSPACE.bazel"):
        d = CASE / ("separate-" + n)
        d.mkdir()
        (d / n).touch()
        (d / "BUILD.bazel").write_text(malformed)
    (CASE / "linked.bzl").symlink_to("nested/BUILD.bazel")
    new = files | {str(NESTED), "WORKSPACE.bazel"}
    inventory(new, "new BUILD.bazel and root WORKSPACE.bazel; nested repositories excluded")
    actual = "//" + str(NESTED.parent) + ":nested"
    q = subprocess.run(["bazel", "--bazelrc=" + os.environ["PROOF_RC"], "query", "--output=label", actual], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(q.returncode == 0 and q.stdout.decode().strip() == actual, "Bazel did not discover real nested BUILD.bazel")
    RESULT["real_package_target"] = actual
    phase("ambient-example-refused", "amd64", new, negative=NESTED)
    require(NESTED.read_text() == malformed, "source check wrote a file")
    parent_config = root.parent / ".buildifier.json"
    require(not parent_config.exists(), "unexpected fixture parent config")
    parent_config.write_text("invalid parent configuration\n")
    # The fix ignores this parent, even when the bypass variable is unset.
    phase("fix-ignores-undeclared-parent", "amd64", new, op="run", fix_env="-u BUILDIFIER_CONFIG")
    sorted_build = UNSORTED.replace('"z.cc",\n        "a.cc"', '"a.cc",\n        "z.cc"')
    require(NESTED.read_text() == sorted_build, "workspace fix used its parent's config")
    NESTED.write_text(malformed)
    Path(".buildifier.json").write_text(CONFIGURED)
    inventory(new | {".buildifier.json"}, "declared first-party root config")
    phase("fix-uses-root-despite-ambient-example", "amd64", new, op="run", fix_env="BUILDIFIER_CONFIG=example")
    require(NESTED.read_text() == UNSORTED and not git("diff", "--name-only"), "fix didn't honor root config")
    phase("root-config-with-ambient-example", "arm64", new)
    Path(".buildifier.json").write_text('{"format":"json"}\n')
    phase("root-config-only-change-rejects-nested", "arm64", new, negative=NESTED)
    require(NESTED.read_text() == UNSORTED and (ignored/"BUILD.bazel").read_text() == malformed, "unexpected source rewrite")
    RESULT["controls"] = {"root_workspace":file_id(Path("WORKSPACE.bazel").read_bytes()), "nested":str(NESTED), "malformed":file_id(malformed.encode()), "default_fix":file_id(sorted_build.encode()), "config_fix":file_id(UNSORTED.encode()), "ignored_parent":file_id(parent_config.read_bytes()), "nested_exclusions":[".git","MODULE.bazel","REPO.bazel","WORKSPACE","WORKSPACE.bazel","symlink"]}
    RESULT.update(verdict="PASS")
    print("Declared configuration and alternate Bazel names verified.")


try:
    main()
except Exception as e:
    RESULT["error"] = {"class": type(e).__name__, "reason": str(e)[:220] if isinstance(e, RuntimeError) else None}
    raise
finally:
    RESULT["verified_at"] = now()
    (TEMP / "buildifier-result.json").write_text(json.dumps(RESULT, sort_keys=True, indent=2) + "\n")
