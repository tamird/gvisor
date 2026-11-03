"""Exercise the real NVIDIA ABI parser with Bazel 9 and the two supplier backends."""

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT = Path(os.environ["RUNNER_TEMP"]) / "parser"
PRODUCT = os.environ["PRODUCT"]
OWNER = os.environ["OWNER"]
BINARY = OWNER.removesuffix("_test")
SUPPLIER = "17f6a300884d73de0b903316f85c420c45471268"
TESTDIR = "bazel-testlogs/tools/nvidia_driver_differ/driver_ast_parser_test"
BINDIR = "bazel-bin/tools/nvidia_driver_differ/driver_ast_parser"
BUNDLE = ROOT / "records/integration.json"
REPORT = {"source": PRODUCT, "supplier": SUPPLIER, "compiler_base": "a385bd2b0155b82faf4fc6a0c195a153ad57550c",
          "bazel_change": "514a1ce4f7dfeca8077048be5d1f321f72a7e2bd", "bazel": "9.3.1",
          "owner": OWNER, "verdict": "INCOMPLETE", "arms": {}}
ARCHS = {
    "x86_64": ("amd64", 62, "78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d"),
    "aarch64": ("arm64", 183, "b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"),
}
BASE = ["docker", "exec", "gnu", str(ROOT / "bazel"), "--bazelrc=" + str(ROOT / "bb.rc")]
CLASSES = {"analysis": "Analysis of target", "missing_package": "no such package", "bad_load": "does not contain symbol",
           "unknown_flag": "Unrecognized option", "unknown_target": "no such target", "incompatible": "incompatible",
           "toolchain": "No matching toolchains", "repository_fetch": "Error downloading",
           "integrity": "Checksum was", "repository_mapping": "repository mapping", "extension": "module extension",
           "no_space": "No space left on device", "memory": "RESOURCE_EXHAUSTED", "killed": "Killed",
           "timeout": "Deadline Exceeded", "no_worker": "No registered executors", "link": "undefined reference",
           "compile": "compilation terminated", "runtime_loader": "shared libraries", "missing_input": "No such file or directory"}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    BUNDLE.write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def captured(command):
    return subprocess.check_output(command, stderr=subprocess.DEVNULL).decode().strip()


def diagnose(path):
    found, locations = set(), []
    for line in path.open(errors="replace"):
        tail = line[:6000]
        found.update(name for name, needle in CLASSES.items() if needle.casefold() in tail.casefold())
        if "error" in tail.lower() or "fail" in tail.lower():
            for match in re.findall(r"(?<![\w/.+-])([\w/+.~-]{1,220}(?:[.]bzl|[.]cc|[.]h|[.]cpp|/BUILD(?:[.]bazel)?|MODULE[.]bazel):\d+(?::\d+)?)", tail):
                if match not in locations and len(locations) < 10:
                    locations.append(match)
    return {"classes": sorted(found), "locations": locations}


def invoke(row, phase, arch, command, arguments, stdout, stderr, seconds, common):
    iid = str(uuid4())
    current = row[phase] = {"invocation": iid, "started": now()}
    save()
    with stdout.open("wb") as out, stderr.open("wb") as err:
        ret = subprocess.run(["timeout", "-k", "30", str(seconds), *BASE, command, "--config=" + arch,
                              "--invocation_id=" + iid, *common, *arguments], stdout=out, stderr=err).returncode
    current.update(exit=ret, finished=now())
    if ret:
        current.update(diagnose(stderr))
    save()
    return ret


def run(arch, properties):
    cpu, machine, image = properties
    arm = REPORT["arms"][arch] = {"verdict": "INCOMPLETE", "expected_machine": machine}
    top = ROOT / arch
    folder = top / "parser"
    records = folder / "records"
    records.mkdir(parents=True)
    save()
    common = ["--jobs=90", "--remote_retries=1", "--noshow_progress", "--lockfile_mode=off", "--use_target_platform_for_tests",
              "--remote_exec_header=x-buildbuddy-platform.estimatedmemory=8000000000",
              "--remote_default_exec_properties=Arch=" + cpu, "--remote_default_exec_properties=OSFamily=linux",
              "--remote_default_exec_properties=workload-isolation-type=oci",
              "--remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/ubuntu@sha256:" + image]
    expr = f'mnemonic("CppCompile", {BINARY}) union mnemonic("TestRunner", {OWNER})'
    graphfile = folder / "graph.json"
    if invoke(arm, "analysis", arch, "aquery", ["--output=jsonproto", "--noinclude_artifacts", expr],
              graphfile, folder / "analysis.log", 900, common):
        return
    graph = json.loads(graphfile.read_bytes())
    compiled = []
    test_platforms = []
    for action in graph["actions"]:
        args = action.get("arguments", [])
        if action["mnemonic"] == "TestRunner":
            test_platforms.append(action["executionPlatform"])
        if action["mnemonic"] != "CppCompile" or not any(x.endswith("/driver_ast_parser.cc") for x in args):
            continue
        compilers = [x for x in args if re.fullmatch(r"(?:external|bazel-out)/[\w+./~=-]+/clang(?:\+\+)?", x)]
        triples = [x for x in args if re.fullmatch(r"(?:--?target=)?(?:aarch64|x86_64)-[\w.\-]+", x)]
        compiled.append({"key": action["actionKey"], "compilers": compilers, "target_arguments": triples,
                         "argv0": args[0] if args and re.fullmatch(r"[\w+./~=-]+", args[0]) else None,
                         "execution_platform": action["executionPlatform"]})
    arm.update(compile_actions=compiled, test_platforms=test_platforms)
    repopath = captured([*BASE, "info", "output_base"])
    inspect = '''import json,sys
from pathlib import Path
base=Path(sys.argv[1])/"external"
matches=[p/"llvm"/"targets.bzl" for p in base.iterdir() if p.name.startswith("llvm+") and p.name.endswith("+llvm-project") and (p/"llvm"/"targets.bzl").is_file()]
assert len(matches)==1
print(json.dumps([matches[0].parents[1].name,matches[0].read_text()]))'''
    name, contents = json.loads(captured(["docker", "exec", "gnu", "python3", "-c", inspect, repopath]))
    line, = contents.strip().splitlines()
    key, val = line.split(" = ", 1)
    backends = ast.literal_eval(val)
    arm.update(repository=name, backend_file_sha256=hashlib.sha256(contents.encode()).hexdigest(), backends=backends)
    save()
    if not (key == "llvm_targets" and sorted(backends) == ["AArch64", "X86"] and compiled
            and all(x["compilers"] and all("llvm" in c.lower() for c in x["compilers"])
                    and any(t.removeprefix("--target=").removeprefix("-target=").startswith(arch + "-") for t in x["target_arguments"])
                    for x in compiled) and len(test_platforms) == 1
            and test_platforms[0].endswith("//tools/bazeldefs:linux_" + cpu)):
        arm["blocker"] = "backend/compiler/target selection"
        save()
        return
    (folder / "source").write_text(PRODUCT)
    (folder / "start").write_text(now())
    under = '/bin/sh -c \'printf "NATIVE_WORKER_ARCH=%s\\n" "$(uname -m)"; test "$(uname -m)" = ' + arch + ' || exit 125; exec "$@"\' native-parser'
    rc = invoke(arm, "test", arch, "test", ["--strategy=TestRunner=remote", "--nocache_test_results",
                "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
                "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1", "--run_under=" + under,
                "--remote_download_outputs=toplevel", "--remote_download_regex=.*/tools/nvidia_driver_differ/driver_ast_parser$",
                "--build_event_json_file=" + str(folder / "bep"), OWNER], folder / "stdout", folder / "build.log", 5700, common)
    (folder / "finish").write_text(arm["test"]["finished"])
    (folder / "exit").write_text(str(rc))
    for f in ("test.xml", "test.log"):
        with (records / f).open("wb") as out:
            subprocess.run(["docker", "exec", "gnu", "cat", TESTDIR + "/" + f], stdout=out, stderr=subprocess.DEVNULL)
    reporter = subprocess.run([sys.executable, "-B", str(ROOT / "parser_owner.py")],
                              env={**os.environ, "RUNNER_TEMP": str(top)}, stdout=subprocess.DEVNULL).returncode
    evidence = json.loads((records / "verdict.json").read_bytes())
    arm["test_result"] = evidence
    log = (records / "test.log").read_bytes()
    arm["native_marker"] = ("NATIVE_WORKER_ARCH=" + arch).encode() in log.splitlines()
    arm["passed_named_case"] = bool(re.search(rb"(?m)^\s*--- PASS: TestParser \(", log))
    arm["verdict"] = "FAIL" if rc else "INCOMPLETE"
    save()
    if rc or reporter or evidence.get("invocation") != arm["test"]["invocation"] or not (arm["native_marker"] and arm["passed_named_case"]):
        return
    # Check the executable that the owning test receives as its data dependency.
    elf = '''import hashlib,json,sys
with open(sys.argv[1],"rb") as f:
 h=f.read(20); f.seek(0); d=hashlib.sha256()
 while b:=f.read(1048576): d.update(b)
 print(json.dumps([h[:6].hex(),int.from_bytes(h[18:20],"little"),d.hexdigest(),f.tell()]))'''
    magic, actual, sha, size = json.loads(captured(["docker", "exec", "gnu", "python3", "-c", elf, BINDIR]))
    arm["elf"] = {"machine": actual, "sha256": sha, "bytes": size}
    if magic == "7f454c460201" and actual == machine and size > 0:
        arm["verdict"] = "PASS"
    save()


assert captured(["git", "rev-parse", "HEAD"]) == PRODUCT
for arch, props in ARCHS.items():
    try:
        run(arch, props)
    except (AssertionError, KeyError, OSError, ValueError, subprocess.CalledProcessError) as err:
        REPORT["arms"][arch]["collector_error"] = type(err).__name__
        save()
REPORT["exact_source_unchanged"] = (captured(["git", "rev-parse", "HEAD"]) == PRODUCT
                                    and not captured(["git", "diff", "--name-only", "HEAD", "--"]))
REPORT["verified_at"] = now()
REPORT["verdict"] = "PASS" if REPORT["exact_source_unchanged"] and all(REPORT["arms"][arch]["verdict"] == "PASS" for arch in ARCHS) else "INCOMPLETE"
if any(a.get("test", {}).get("exit") or a.get("analysis", {}).get("exit") for a in REPORT["arms"].values()):
    REPORT["verdict"] = "FAIL"
save()
raise SystemExit(REPORT["verdict"] != "PASS")
