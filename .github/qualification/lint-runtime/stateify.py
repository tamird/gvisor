"""Verify the published release, declared inputs and import binding controls."""

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile

import check as harness

SOURCE = "778375bc602d883faf89a2f5b7bdf4016804da10"
BASE = "471d6dff5229b99208bf6fec985601067f1f9d69"
EXPORT = "1899f71d79be9b78ed439313ee050ff6b8d62a5d"
STOCK_ZIP = "5161da9ab19357a8434690839579212cddeb6179635a23cd2766383b5f72415e"
ROOT = harness.ROOT
REPORT = ROOT / "stateify-native-review.json"
OUT = {"source": SOURCE, "base": BASE, "export": EXPORT, "started": harness.utc(),
       "verdict": "INCOMPLETE", "phases": {}}
OLD = ("generated", "imports", "unaliased", "declared", "versioned")
EXTRA = ("odd_implicit", "odd_declared", "odd_aliased", "generic", "literal", "identtype", "ambiguous")
EXPECTED_BAD = {"odd_implicit", "odd_declared", "ambiguous"}
OLD_PRODUCTION = ("//pkg/bpf:bpf", "//pkg/ebpf:ebpf", "//pkg/sentry/fsimpl/overlay:overlay",
                  "//pkg/sentry/fsimpl/tmpfs:tmpfs", "//pkg/sentry/kernel:kernel", "//pkg/sentry/mm:mm",
                  "//pkg/sentry/socket/netstack:netstack")
check = harness.check


def save():
    REPORT.write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")


def phase(name, fn):
    row = OUT["phases"][name] = {"verdict": "INCOMPLETE", "started": harness.utc()}
    save()
    try:
        fn(row)
        row["verdict"] = "PASS"
    except Exception as exc:
        row["verdict"] = "FAIL"
        row["exception"] = type(exc).__name__
        if isinstance(exc, (AssertionError, ValueError)):
            row["reason"] = str(exc)[:200]
    finally:
        row["verified_at"] = harness.utc()
        save()


def label(short):
    name = "qualification_" + short
    return "//tools/go_stateify/" + name + ":" + name


def setup(row):
    carrier = Path(os.environ["CARRIER_DIR"]) / "tools/go_stateify"
    for path in sorted(carrier.glob("qualification_*")):
        shutil.copytree(path, Path("tools/go_stateify") / path.name)
    files = {str(p.relative_to(carrier.parent.parent)): harness.digest(p.read_bytes())
             for p in carrier.rglob("*") if p.is_file()}
    archive = ROOT / "stateify-control.zip"
    check(harness.digest(archive.read_bytes()) == STOCK_ZIP, "wrong retained ZIP")
    with zipfile.ZipFile(archive) as bundle:
        retained = json.loads(bundle.read("stateify-native-review.json"))
    check(retained["verdict"] == "PASS" and retained["base"] == BASE
          and retained["phases"]["stock_gnu"]["source"] == BASE, "incompatible stock")
    check(all(files.get(p) == digest for p, digest in retained["fixture_files"].items()), "old fixture changed")
    row.update(stock_artifact=13251728088, zip_sha256=STOCK_ZIP, fixture_hashes=files,
               gnu=retained["phases"]["stock_gnu"]["completed"],
               stock_outputs={label: a["digest"] for label, a in retained["phases"]["stock_gnu"]["actions"].items()})
    check(harness.git("rev-parse", SOURCE+"^").decode().strip() == BASE, "wrong base")
    check("471d6dff5" in harness.git("show", "-s", "--format=%s", EXPORT).decode(), "wrong Go release")
    check(len(row["stock_outputs"]) == 12 and len(row["gnu"]) == 12, "missing stock results")


def first():
    data = (ROOT / "first.zip").read_bytes()
    check(harness.digest(data) == "88d331cf2fbb3f43f40a998aff9a1c83e64ac8c612524dfeccd90b9519454a40", "wrong first report")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return json.loads(archive.read("stateify-native-review.json"))


def original_console():
    sys.path.insert(0, str(ROOT))
    from cas_inputs import fetch

    previous = first()["phases"]["exported_tree"]
    identifier = previous["invocation"]
    check(identifier == "da8b6ac2-7814-491c-9bb8-7e4ef7337838" and previous["exact"] == 480
          and len(previous["missing"]) == 5 and not previous["changed"], "wrong original failure")
    result = {"invocation": identifier, "original_exit": previous["exit"], "diagnostic_available": False, "classes": []}
    try:
        token = "0"
        pieces = []
        for _ in range(8):
            page = json.loads(fetch("api/v1/GetLog", {"selector": {"invocation_id": identifier}, "page_token": token}))
            pieces.append(page.get("log", {}).get("contents", ""))
            check(sum(map(len, pieces)) < 8 * 1024**2, "original console exceeds limit")
            token = page.get("next_page_token", page.get("nextPageToken"))
            if not token:
                break
        log = "".join(pieces)
        result.update(diagnostic_available=bool(log), complete=not token, digest=harness.digest(log.encode()),
                      classes=diagnoses(log))
    except Exception as exc:
        result["unavailable"] = type(exc).__name__
    (ROOT / "original-diagnostics.json").write_text(json.dumps(result))


def diagnoses(log):
    classes = {"stdlib_undeclared_testpty": r"internal/testpty", "no_strategy": r"NO_USABLE_STRATEGY_FOUND|No usable strategy",
               "nospace": r"No space left on device", "oom": r"OutOfMemoryError|Cannot allocate memory|oom.kill|Killed signal",
               "sandbox": r"sandbox violation|Operation not permitted|Permission denied",
               "bash_absent": r"(?:/bin/bash|/bin/sh).*?(?:not found|No such file)|(?:not found|No such file).*?(?:/bin/bash|/bin/sh)",
               "abi_input": r"cannot find package|could not import", "exec_format": r"Exec format error",
               "qemu": r"qemu[^\s]*: (?:error|Could not|failed)", "remote_env": r"FailedPrecondition|remote executor",
               "compiler_missing": r"gcc.*(?:not found|No such file)|clang.*(?:not found|No such file)",
               "remote_fetch": r"Error downloading|UNAVAILABLE|HTTP (?:429|503)", "genrule": r"Executing genrule.*failed",
               "go_compile": r"GoCompilePkg.*failed|compilepkg: error", "go_link": r"GoLink.*failed|link: error",
               "nogo": r"GoStaticAnalysis.*failed|nogo:|FilterStdPackages",
               "process_setup": r"process-wrapper:.*(?:error|failed)|error executing command",
               "action_input_missing": r"missing input file|does not exist", "opt_visibility": r"not visible from target"}
    return sorted(name for name, pattern in classes.items() if re.search(pattern, log, re.I))


def build(row, phase_name, source, labels, *, minimal, focused=False, changed=(), test=False):
    subprocess.run(["git", "checkout", "--detach", source], check=True, stdout=subprocess.DEVNULL)
    check(harness.git("rev-parse", "HEAD").decode().strip() == source and set(harness.git("diff", "--name-only").decode().splitlines()) == set(changed),
          "incorrect product source")
    bep = ROOT / (phase_name + ".bep")
    spawns = ROOT / (phase_name + ".exec")
    row.update(source=source, invocation=str(uuid.uuid4()), expected=len(labels))
    image = harness.IMAGE if minimal else harness.GNU_IMAGE
    args = ["bazel", "--bazelrc=" + os.environ["QA_RC"], "test" if test else "build", "--config=x86_64", "--jobs=16", "--keep_going",
            "--extra_execution_platforms=//tools/bazeldefs:linux_amd64", "--spawn_strategy=local",
            "--strategy=GoStateify=remote", "--strategy=GoImports=remote", "--remote_default_exec_properties=Arch=amd64",
            "--remote_default_exec_properties=OSFamily=linux", "--remote_default_exec_properties=recycle-runner=false",
            "--remote_default_exec_properties=workload-isolation-type=" + ("oci" if minimal else "firecracker"),
            "--remote_default_exec_properties=container-image=" + image, "--remote_download_regex=.*_state_autogen.*[.]go$",
            "--invocation_id="+row["invocation"], "--build_event_json_file="+str(bep)]
    if test:
        args.extend(["--test_filter=^TestLoadHooks$", "--nocache_test_results", "--strategy=TestRunner=local",
                     "--test_output=errors", "--test_arg=-test.v", "--nobuild_event_json_file_path_conversion"])
    if phase_name == "release5":
        # These targets also build the tool that generates precompiled seccomp code.
        # Select its normal GNU execution platform. If the original emitted the SDK
        # mismatch, use the established declared-input strategies only for those SDK actions.
        args.extend(["--spawn_strategy=remote", "--strategy=GoStateify=remote"])
        if "stdlib_undeclared_testpty" in OUT["original_diagnostics"]["classes"]:
            args.extend(["--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
                         "--modify_execution_info=GoStdlib(List)?=+no-remote-cache"])
    if focused:
        args.extend(["--modify_execution_info=^GoStateify$=+no-cache", "--execution_log_json_file="+str(spawns)])
    cmd = subprocess.run(args + sorted(labels), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    row.update(exit=cmd.returncode, finished=harness.utc(), log_sha256=harness.digest(cmd.stdout))
    log = cmd.stdout.decode(errors="replace")
    row["classes"] = diagnoses(log)
    row["diagnostics"] = [line[:400] for line in log.splitlines()
                          if re.search(r"undefined:|no import for|generated code uses|no such target|no such package|syntax error|not used|invalid import path|cannot use|cannot find package", line)
                          and not re.search(r"(?i)(token|key=|authorization|credential|://)", line)][:30]
    row["completed"] = {event["id"]["targetCompleted"]["label"]: event["completed"].get("success", False)
                        for event in harness.rows(bep) if "targetCompleted" in event.get("id", {})}
    save()
    if focused:
        actions = row["generator_actions"] = {}
        for spawn in harness.rows(spawns):
            check(spawn.get("mnemonic") != "GoImports", "formatter executed")
            if spawn.get("mnemonic") != "GoStateify" or spawn.get("targetLabel") not in ({l+"_state_autogen" for l in labels} | labels):
                continue
            target = spawn["targetLabel"]
            command = spawn["commandArgs"]
            props = {p["name"]: p["value"] for p in spawn.get("platform", {}).get("properties", [])}
            outputs = spawn.get("actualOutputs", [])
            row2 = actions[target] = {"runner": spawn.get("runner"), "cached": spawn.get("cacheHit", False),
                                      "exit": spawn.get("exitCode", 0), "tool": command[0], "image": props.get("container-image")}
            check(row2["runner"] == "remote" and not row2["cached"] and props.get("container-image") == image
                  and props.get("workload-isolation-type") == "oci" and Path(command[0]).name == "stateify",
                  "generator did not run directly on shell-free worker")
            if row2["exit"] == 0:
                generated, = outputs
                content = Path(generated["path"]).read_bytes()
                row2["digest"] = harness.digest(content)
                if target == label("identtype")+"_state_autogen":
                    check(b'peer "gvisor.dev/gvisor/pkg/abi/linux"' in content
                          and b'example.invalid' not in content and b'peer.BPFInstruction' in content,
                          "identtype retargeted its Go source import")
                check(row2["digest"] == generated["digest"]["hash"], "wrong materialized generator output")
        save()
    return log


def fixtures(row):
    production = {p+"_state_autogen" for p in OLD_PRODUCTION}
    libs = {label(k) for k in OLD + EXTRA}
    log = build(row, "candidate", SOURCE, production | libs, minimal=True, focused=True)
    check(set(row["completed"]) == production | libs and row["exit"] == 1, "candidate owners missing")
    bad = {label(k) for k in EXPECTED_BAD}
    check({l for l, v in row["completed"].items() if not v} == bad, "unexpected candidate dispositions")
    check("generated code uses \"peer\" for both" in log and "no import for \"shapes\"" in log, "wrong negative")
    actions = row["generator_actions"]
    expected = production | {l+"_state_autogen" for l in libs}
    check(expected == set(actions), "missing or unexpected generator action")
    before = OUT["phases"]["retained"]["stock_outputs"]
    identical = production | {label(k)+"_state_autogen" for k in ("declared", "unaliased")}
    check(all(actions[l]["digest"] == before[l] for l in identical), "previously correct output changed")
    row["unchanged_from_stock"] = sorted(identical)
    old = OUT["phases"]["stock_unusual_names"].get("completed", {})
    row["stock_name_regressions"] = [l for l, passed in old.items() if passed and not row["completed"].get(l)]
    check(old and not row["stock_name_regressions"], "an unaliased name accepted by stock needs compatibility")


FIXTURE_SOURCES = ("pkg/state/tests/load.go", "pkg/state/tests/struct.go")
SECCOMP = {"pkg/sentry/platform/kvm": "mmapfilter_precompiled.go",
           "pkg/sentry/platform/ptrace": "stubfilter_precompiled.go",
           "pkg/sentry/platform/systrap": "stubfilter_precompiled.go",
           "runsc/boot/filter": "filter_precompiled.go", "runsc/gofer/fs/filter": "filter_precompiled.go"}


def test_hook(row):
    sys.path.insert(0, str(ROOT))
    from qualification_report import xml_cases

    owner = "//pkg/state/tests:tests_test"
    build(row, "hook", SOURCE, {owner}, minimal=True, test=True)
    check(row["exit"] == 0 and row["completed"].get(owner), "state test owner failed")
    results = [e for e in harness.rows(ROOT / "hook.bep") if e.get("id", {}).get("testResult", {}).get("label") == owner]
    attempt, = results
    result = attempt["testResult"]
    row["attempt"] = attempt["id"]["testResult"]
    row["status"] = result["status"]
    row["cached"] = result.get("cachedLocally", False) or result.get("executionInfo", {}).get("cachedRemotely", False)
    check(row["status"] == "PASSED" and not row["cached"], "named test did not execute")
    xml = Path("bazel-testlogs/pkg/state/tests/tests_test/test.xml")
    names = {"/".join(filter(None, (c.get("classname"), c.get("name")))) for c in ET.parse(xml).iter("testcase")
             if "TestLoadHooks" in c.get("name", "").split("/")}
    counts = xml_cases(xml, frozenset(names))
    log = xml.with_name("test.log").read_bytes()
    started = bool(re.search(rb"(?m)^=== RUN\s+TestLoadHooks\s*$", log))
    passed = bool(re.search(rb"(?m)^--- PASS: TestLoadHooks \(", log))
    row.update(xml_sha256=harness.digest(xml.read_bytes()), log_sha256=harness.digest(log), cases=counts["passed"],
               load_hook_xml_cases=len(names), go_run=started, go_pass=passed)
    check(counts["passed"] > 0 and counts["failed"] == 0 and counts["skipped"] == 0 and
          (names and set(counts["required_passed"]) == names or started and passed), "missing executed Go load hook cases")


@contextmanager
def reference_inputs(row, files=(), packages=()):
    # Hold the inputs fixed for release byte comparisons. The one intentionally
    # changed test fixture is also tested separately on its actual product source.
    changed, originals, created = [], {}, []
    try:
        for name in files:
            p = Path(name)
            originals[p] = p.read_bytes()
            p.write_bytes(harness.git("show", BASE + ":" + name))
            changed.append(name)
        row["original_fixture_inputs"] = {name: harness.digest(Path(name).read_bytes()) for name in files}
        for pkg in packages:
            filename = SECCOMP[pkg]
            b = Path(pkg) / "BUILD"
            ref = Path(pkg) / filename
            check(not ref.exists(), "unexpected existing seccomp input")
            originals[b] = b.read_bytes()
            def remove_macro(match):
                check('out = "'+filename+'"' in match.group(), "incorrect seccomp output")
                return ""
            text, n = re.subn(r"(?ms)^precompiled_seccomp_rules\(\n.*?^\)\n", remove_macro, b.read_text())
            check(n == 1 and '"'+filename+'"' in text, "incorrect seccomp declaration")
            b.write_text(text)
            changed.append(str(b))
            data = harness.git("show", EXPORT+":"+str(ref))
            ref.write_bytes(data)
            created.append(ref)
            row.setdefault("exported_generated_inputs", {})[str(ref)] = harness.digest(data)
        yield changed
    finally:
        for p in created:
            p.unlink()
        for p, data in originals.items():
            p.write_bytes(data)


def all_outputs(row, failed, supplied=False):
    paths = [s for s in harness.git("ls-tree", "-r", "--name-only", EXPORT).decode().splitlines()
             if s.endswith("_state_autogen.go")]
    check(len(paths) == 485, "wrong exported census")
    targets = {"//"+str(Path(p).parent)+":"+Path(p).stem: p for p in paths}
    prior = set(first()["phases"]["exported_tree"]["missing"])
    targets = {t: p for t, p in targets.items() if (t in prior) == failed}
    if supplied:
        missing = set(OUT["phases"]["five_with_seccomp_inputs"]["missing"])
        targets = {t: p for t, p in targets.items() if t in missing}
    packages = sorted(str(Path(p).parent) for p in targets.values()) if supplied else []
    with reference_inputs(row, () if failed else FIXTURE_SOURCES, packages) as changed:
        name = "supplied5" if supplied else ("release5" if failed else "release480")
        build(row, name, SOURCE, targets, minimal=not failed or supplied, changed=changed)

    rows = row["files"] = {}
    for target, path in targets.items():
        content = harness.git("show", EXPORT+":"+path)
        check(b"stateify" in content.splitlines()[0], "wrong reference generator")
        actual = Path("bazel-bin") / path
        rows[target] = {"reference": harness.digest(content), "generated": harness.digest(actual.read_bytes())
                        if row["completed"].get(target) and actual.is_file() else None}
    row["exact"] = sum(d["generated"] == d["reference"] for d in rows.values())
    row["missing"] = [l for l, d in rows.items() if d["generated"] is None]
    row["changed"] = [l for l, d in rows.items() if d["generated"] and d["generated"] != d["reference"]]
    save()
    check(row["exit"] == 0 and set(row["completed"]) == set(targets) and row["exact"] == len(targets),
          "exported bytes or coverage differs")


if len(sys.argv) == 2 and sys.argv[1] == "original":
    original_console()
    raise SystemExit(0)
OUT["original_diagnostics"] = json.loads((ROOT / "original-diagnostics.json").read_bytes())
phase("retained", setup)
if OUT["phases"]["retained"]["verdict"] == "PASS":
    OUT["phases"]["stock_unusual_names"] = first()["phases"]["stock_unusual_names"]
    OUT["phases"]["stock_unusual_names"]["retained_artifact"] = 13253469180
    phase("candidate_imports", fixtures)
    phase("in_tree_sibling_hook", test_hook)
    phase("exported_tree_480", lambda r: all_outputs(r, False))
    phase("five_with_seccomp_inputs", lambda r: all_outputs(r, True))
    if OUT["phases"]["five_with_seccomp_inputs"].get("missing"):
        phase("seccomp_exported_inputs_only", lambda r: all_outputs(r, True, True))
OUT["verdict"] = "PASS" if all(p["verdict"] == "PASS" for p in OUT["phases"].values()) else "FAIL"
OUT["finished"] = harness.utc()
save()
raise SystemExit(OUT["verdict"] != "PASS")
