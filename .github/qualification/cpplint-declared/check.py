"""Reconcile the originals of one shell-free lint action and its test alias."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

SOURCE = "b4720d1975646e4d202ba3f59b7b835cedcdee3b"
TREE = "2be90e91b6e92263f27ac16b23f80fa0b9152609"
CHECKER = "f3920e47d89eba739f866405745ce6b88db1395b8dacf83d880bac95043fa8d5"
STATIC = "02939080d36c0fd2dc2507ff146a4c86360fa3968f30e1e42a6424d352629394"
IMAGES = {
    "scratch": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
    "ubuntu": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
}
P = Path(os.environ["RUNNER_TEMP"]) / "cpplint-evidence"
P.mkdir()
CASE = Path("test/util/.cpplint_declared_control")
NEW = str(CASE / ".new file.cc")
CONFIG = str(CASE / "CPPLINT.cfg")
RAW = Path(os.environ["RUNNER_TEMP"]) / "cpplint-originals"
RAW.mkdir()
RESULT = {"verdict": "INCOMPLETE", "source": SOURCE, "tree": TREE, "phases": {}}


def checked(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return sha256(data).hexdigest()


def held(path):
    data = path.read_bytes()
    return {"sha256": digest(data), "bytes": len(data)}


def git(*args):
    return subprocess.check_output(["git", *args])


def records(path):
    value = path.read_text()
    decoder = json.JSONDecoder()
    while value:
        value = value.lstrip()
        if not value:
            break
        row, end = decoder.raw_decode(value)
        yield row
        value = value[end:]


def phase(name, platform, sources, configs, negative=False, test=False):
    d = RAW / name
    d.mkdir()
    row = {"started": utc(), "phase": name, "platform": platform}
    RESULT["phases"][name] = row
    be, execution, log = d / "events.jsonl", d / "execution.json", d / "combined.log"
    label = "//:cpplint_test" if test else "//:cpplint"
    platform_label = "@cpplint_qualification//:" + platform
    argv = [
        "bazel", "--bazelrc=" + os.environ["CPPLINT_RC"], "test" if test else "build",
        "--host_platform=" + platform_label, "--extra_execution_platforms=" + platform_label,
        "--platforms=//tools/bazeldefs:linux_amd64",
        "--extra_toolchains=@cpplint_qualification//:static_python",
        "--noremote_accept_cached", "--build_event_json_file=" + str(be),
        "--execution_log_json_file=" + str(execution),
        *(["--nocache_test_results"] if test else []), label,
    ]
    with log.open("wb") as stream:
        native = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, check=False)
    row.update(exit=native.returncode, finished=utc(), log=held(log))
    data = log.read_text(errors="replace")
    # Keep the operation's original diagnostic local; this public selection
    # contains only filename/line/category for the deliberate source error.
    events = list(records(be)) if be.exists() else []
    row["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
    row["source_diagnostics"] = re.findall(re.escape(NEW) + r":[^\n]*?\[build/include_order\][^\n]*", data)
    row["first_errors"] = [
        line[:700] for line in data.splitlines() if line.startswith("ERROR:")
        and not any(t in line.lower() for t in ("api-key", "authorization", "header=", "token=", "bearer "))
    ][:4]
    checked(execution.exists(), "no execution log")
    runs = list(records(execution))
    owns = [s for s in runs if s.get("mnemonic") == "Cpplint" and s.get("targetLabel") == "//:cpplint"]
    checked(len(owns) == 1, "expected one native Cpplint spawn")
    spawn = owns[0]
    runargv = spawn["commandArgs"]
    physical = {f["path"]: f for f in spawn["inputs"]}
    img = {p["name"]: p["value"] for p in spawn["platform"]["properties"]}
    row.update(runner=spawn.get("runner"), cache_hit=spawn.get("cacheHit", False), worker_platform=img,
               cpplint_exit=spawn.get("exitCode", 0), command=runargv[:6], execution=held(execution))
    checked(row["runner"] == "remote" and not row["cache_hit"], "Cpplint was not freshly executed remotely")
    checked(img.get("container-image") == IMAGES[platform] and img.get("Arch") == "amd64", "unexpected worker placement")
    checked(row["cpplint_exit"] == int(negative) and native.returncode == int(negative), "unexpected native exit")
    program, isolated, driver, output, main, root = runargv[:6]
    checked(isolated == "-IB" and driver == "tools/cpplint.py" and root.endswith("/src/ROOT"), "unexpected command")
    checked(physical[program]["digest"]["hash"] == STATIC, "wrong actual interpreter")
    checked(physical[main]["digest"]["hash"] == CHECKER, "wrong actual supplier main")
    checked(physical[driver]["digest"]["hash"] == digest(Path(driver).read_bytes()), "wrong checker driver")
    row.update(interpreter=physical[program]["digest"], checker=physical[main]["digest"], driver=physical[driver]["digest"])
    prefix = root.removesuffix("ROOT")
    observed = {p.removeprefix(prefix): f for p, f in physical.items() if p.startswith(prefix)}
    checked(set(observed) == sources | configs | {"ROOT"}, "source or configuration inputs differ")
    checked(set(runargv[6:]) == sources and len(runargv[6:]) == len(sources), "checker arguments differ")
    facts = {}
    for path in sorted(sources | configs):
        expected = Path(path).read_bytes()
        h = observed[path]["digest"]
        checked(h["hash"] == digest(expected) and int(h.get("sizeBytes", 0)) == len(expected), "physical input bytes differ: " + path)
        facts[path] = h["hash"]
    row.update(source_count=len(sources), configs={p:facts[p] for p in sorted(configs)},
               source_membership_sha256=digest(("\0".join(sorted(sources)) + "\0").encode()),
               content_map_sha256=digest(json.dumps(facts, sort_keys=True).encode()),
               output_files=spawn.get("actualOutputs", []), no_workspace_vcs=not any(".git" in Path(p).parts for p in physical))
    checked(row["no_workspace_vcs"], "VCS included")
    checked(bool(row["source_diagnostics"]) == negative, "wrong failure diagnostic")
    if negative:
        checked(len(row["source_diagnostics"]) == 1 and row["source_diagnostics"][0].startswith(NEW + ":3:"), "wrong source/line")
        checked(not row["output_files"], "failing lint produced success output")
    else:
        outs = row["output_files"]
        checked(len(outs) == 1 and outs[0]["path"] == output and outs[0]["digest"]["hash"] == digest(b""), "success output not found")
    if test:
        tests = [e for e in events if e.get("id", {}).get("testResult", {}).get("label") == label]
        check_rows = [s for s in runs if s.get("mnemonic") == "TestRunner"]
        row["test_results"] = [t["testResult"].get("status") for t in tests]
        physical = []
        for spawn in check_rows:
            cmd = spawn["commandArgs"]
            physical.append({
                "target": spawn.get("targetLabel"), "runner": spawn.get("runner"),
                "cache_hit": spawn.get("cacheHit", False), "exit": spawn.get("exitCode", 0),
                "test_setup": any(c.endswith("/test-setup.sh") for c in cmd),
                "xml_generator": any("generate-xml" in c for c in cmd),
                "output": {Path(x["path"]).name: x.get("digest") for x in spawn.get("actualOutputs", [])
                           if x["path"].endswith(("/test.log", "/test.xml"))},
            })
        row["test_executions"] = physical
        checked(row["test_results"] == ["PASSED"], "one original TestResult must pass")
        outputs = {}
        for f in tests[0]["testResult"].get("testActionOutput", []):
            name = Path(f["name"]).name
            if name in {"test.log", "test.xml"}:
                match = re.fullmatch(r"bytestream://remote\.buildbuddy\.io/(?:[^?]*/)?blobs/([0-9a-f]{64})/(\d+)", f.get("uri", ""))
                if match:
                    outputs[name] = {"hash": match.group(1), "sizeBytes": match.group(2)}
                elif f.get("digest"):
                    outputs[name] = {"hash": f["digest"], "sizeBytes": str(f["length"])}
                elif (u := urlsplit(f.get("uri", ""))).scheme == "file":
                    path = Path(unquote(u.path))
                    checked(path.name == name and len(path.read_bytes()) <= 4 << 20, "advertised local test report")
                    data = path.read_bytes()
                    outputs[name] = {"hash": digest(data), "sizeBytes": str(len(data))}
        row["test_reports"] = outputs
        checked(set(outputs) == {"test.log", "test.xml"}, "test reports missing")
        native = [p for p in physical if p["runner"] == "remote" and p["exit"] == 0 and not p["cache_hit"]]
        for name, d in outputs.items():
            matches = [p for p in native if p["output"].get(name, {}).get("hash") == d["hash"]
                       and int(p["output"][name].get("sizeBytes", 0)) == int(d["sizeBytes"])]
            checked(any(p["test_setup"] for p in matches) if name == "test.log" else matches, "physical " + name)
    row["verified"] = True
    print(name, row["invocation"], "verified", flush=True)


def main():
    checked(git("rev-parse", "HEAD", "HEAD^{tree}").decode().splitlines() == [SOURCE,TREE], "source identity")
    checked(not git("status", "--porcelain", "--untracked-files=no"), "tracked source changed")
    tracked = {x.decode() for x in git("ls-files", "-z", "--", "*.c", "*.cc", "*.h").split(b"\0") if x}
    checked(len(tracked) == 485, "changed baseline")
    phase("scratch-clean", "scratch", tracked, set())
    CASE.mkdir()
    (CASE / "BUILD").write_text("# No file target or lint hook.\n")
    contents = "// New source, not in the Git index.\n#include <vector>\n#include <stdio.h>\n"
    Path(NEW).write_text(contents)
    outside = RAW / "outside"
    outside.mkdir()
    (outside / "bad.cc").write_text(contents)
    (CASE / "linked").symlink_to(outside, target_is_directory=True)
    (CASE / "linked.cc").symlink_to(outside / "bad.cc")
    for path in (CASE / ".git", Path(".cache/clangd/lint"), CASE / "node_modules", Path("repo/cpplint-generated"), CASE / "nested-module"):
        path.mkdir(parents=True)
        (path / "ignored.cc").write_text(contents)
    (CASE / "nested-module/MODULE.bazel").write_text('module(name="unrelated")\n')
    checked(not git("ls-files", "--", NEW), "new file already in Git index")
    phase("scratch-new-package", "scratch", tracked | {NEW}, set(), negative=True)
    Path(CONFIG).write_text("filter=-build/include_order\n")
    phase("scratch-local-config", "scratch", tracked | {NEW}, {CONFIG})
    phase("ordinary-test", "ubuntu", tracked | {NEW}, {CONFIG}, test=True)
    checked(not git("status", "--porcelain", "--untracked-files=no"), "tracked source changed")
    RESULT["verdict"] = "PASS"


try:
    main()
except Exception as exc:
    RESULT["error"] = {"type": type(exc).__name__, "assertion": str(exc)[:220] if isinstance(exc, RuntimeError) else None}
    raise
finally:
    RESULT["verified_at"] = utc()
    (P / "result.json").write_text(json.dumps(RESULT, sort_keys=True, indent=2) + "\n")
