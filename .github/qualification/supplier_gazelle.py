"""Exercise the supplier runner and an otherwise unchanged external gVisor module."""

from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys

import downstream_llvm as qa


GVISOR = "69e89d7a3dfc6b2b484f7dc01a68fad68741dd03"
MASTER = "8b89ee9f6d3c920911bf6ff73fabd4e86b4b9c3e"
SOURCE_SHA = "f315fe81ebaa871be7fa4da1e86f3432fec5aa94788f096c601a5af2366a5e46"
PATCH_SHA = "09a13a97ad6abcb783110876893adffbcc428914da68ec6cb1c45d56ce3fe95f"
STABLE_DEF = "715752cac2534b09c82a8487b78633219c117625fcc126f90f704ee039dde972"
SUPPLIER = qa.AREA / "gazelle"
PATCH = qa.AREA / "supplier.patch"
NATIVE = "8.2.1"
INHERITED = "inherited value with ' and $HOME"
DIRECT = "default with a space and ; and quoted 'string'"
OVERRIDE = "target overrides the repository"
MARK = "GAZELLE_RUNNER_VERIFIED_DECLARED_ENV"
TESTS = ("valid_env_variables_test", "env_variable_name_cant_start_with_numbers_test", "env_variable_name_cant_be_empty")


def proc(case, name, flavor, op, *args, timeout=640):
    modes = ["--enable_bzlmod", "--noenable_workspace"] if flavor == "bzlmod" else ["--noenable_bzlmod", "--enable_workspace"]
    env = {**os.environ, "USE_BAZEL_VERSION": NATIVE, "GAZELLE_QA_INHERITED": INHERITED}
    cmd = [str(qa.AREA / "bazelisk"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
           op, *modes, "--repo_env=GAZELLE_QA_INHERITED", "--lockfile_mode=off"]
    if op != "query":
        cmd.append("--jobs=3")
    cmd.extend(args)
    try:
        p = subprocess.run(cmd, cwd=SUPPLIER, env=env, capture_output=True, timeout=timeout)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        code, out, err = 124, exc.stdout or b"", exc.stderr or b""
    row = case[name] = {"exit": code, "stdoutSHA256": sha256(out).hexdigest(), "stderrSHA256": sha256(err).hexdigest()}
    if code:
        row["diagnostics"] = [s.replace(str(qa.AREA), "<fixture>")[:500] for s in err.decode(errors="replace").splitlines()
                              if s.startswith("ERROR:") or "Error in fail:" in s or "Cannot fetch" in s][:6]
    qa.save()
    return row, out


def downstream(arm):
    c = qa.REPORT["cases"][arm] = {"source": GVISOR}
    qa.CONSUMER.mkdir(exist_ok=True)
    module = ('module(name = "downstream")\nbazel_dep(name = "gvisor", version = "0.0.1")\n'
              f'local_path_override(module_name = "gvisor", path = {json.dumps(str(qa.SRC))})\n')
    if arm != "stable":
        module += f'local_path_override(module_name = "gazelle", path = {json.dumps(str(SUPPLIER))})\n'
    (qa.CONSUMER / "MODULE.bazel").write_text(module)
    (qa.CONSUMER / ".bazelversion").write_text("9.3.1\n")
    (qa.CONSUMER / "BUILD.bazel").write_text("")
    api = qa.CONSUMER / "old_api"
    api.mkdir(exist_ok=True)
    (api / "BUILD.bazel").write_text('load("@gvisor//tools/bazeldefs:go.bzl", "gazelle")\nfilegroup(name = "marker")\n')
    c["consumerModule"] = module.replace(str(qa.SRC), "<source>").replace(str(SUPPLIER), "<pinned-gazelle>")
    selected = "set(@gvisor//:license @gvisor//:sandbox //old_api:marker)"
    row, out = qa.bazel(c, "metadataAndAPI", "query", selected, "--output=label")
    row["labels"] = sorted(s for s in out.decode(errors="replace").splitlines() if s.startswith(("@", "//")))
    qa.plan(c)
    qa.build(c)
    c["sameKnownELF"] = c["build"].get("elf", {}).get("sha256") == "7e34fe3c975a09c00f3486e7c9a447493b142e7aa02f989e4222d6cb39e7077a"
    info, out = qa.bazel(c, "bazelOutputBase", "info", "output_base")
    if info["exit"] == 0:
        location = Path(out.decode().strip()) / "external/gazelle+/def.bzl"
        if location.is_file():
            c["resolvedGazelleDefSHA256"] = sha256(location.read_bytes()).hexdigest()
            if arm == "stable":
                c["selectedStableVersion"] = 'version = "0.54.0"' in (location.parent / "MODULE.bazel").read_text()[:500]
    expected = STABLE_DEF if arm == "stable" else sha256((SUPPLIER / "def.bzl").read_bytes()).hexdigest()
    c["sourceMatches"] = c.get("resolvedGazelleDefSHA256") == expected and c.get("selectedStableVersion", True)
    c["gvisorModuleSHA256"] = sha256((qa.SRC / "MODULE.bazel").read_bytes()).hexdigest()
    c["wireUpVerified"] = (c["sourceMatches"] and c["sameKnownELF"] and c.get("linkedARM64") and
        c["metadataAndAPI"]["labels"] == ["//old_api:marker", "@gvisor//:license", "@gvisor//:sandbox"] and
        c["configured"].get("counts") == {"CppCompile": 2, "CppLink": 1} and
        all(a["toolFromLlvm"] and a["armTarget"] for a in c["configured"]["actions"]))
    diagnostics = c["build"].get("diagnostics", [])
    c["goPolicyFailure"] = any("go_deps.gazelle_override" in line and "non-root" in line for line in diagnostics)
    qa.save()
    return bool(c["wireUpVerified"])


def setup_supplier():
    env = json.dumps({"GAZELLE_QA_DIRECT": DIRECT, "GAZELLE_QA_OVERRIDDEN": "base", "GAZELLE_QA_EMPTY": "base"})
    kw = 'go_env = ' + env + ', go_env_inherit = ["GAZELLE_QA_INHERITED"]'
    for file, old, new in (
        ("MODULE.bazel", 'go_deps.from_file(go_work = "//:go.work")',
         'go_deps.config(' + kw + ')\ngo_deps.from_file(go_work = "//:go.work")'),
        ("WORKSPACE", 'gazelle_dependencies(go_sdk = "go_sdk")', 'gazelle_dependencies(go_sdk = "go_sdk", ' + kw + ')'),
    ):
        p = SUPPLIER / file
        text = p.read_text()
        assert text.count(old) == 1
        p.write_text(text.replace(old, new))
    # Gazelle excludes bcr_tests from its list of repository tool sources.
    folder = SUPPLIER / "bcr_tests/runner_qa"
    folder.mkdir(parents=True)
    (folder / "BUILD.bazel").write_text(
        'load("//:def.bzl", "gazelle")\n'
        'genrule(name = "read_env", srcs = ["read_env.sh"], outs = ["env_probe"], executable = True, cmd = "cp $(location read_env.sh) $@ && chmod +x $@")\n'
        'gazelle(name = "observe", gazelle = ":read_env", args = ["qa-marker"], env = '
        + json.dumps({"GAZELLE_QA_OVERRIDDEN": OVERRIDE, "GAZELLE_QA_EMPTY": ""}) + ')\n'
        'gazelle(name = "standard", mode = "print", args = ["-index=none", "-r=false", "bcr_tests/runner_qa/sample"])\n')
    shell = ["#!/usr/bin/env bash", "set -euo pipefail"]
    for k, v in {"DIRECT": DIRECT, "INHERITED": INHERITED, "OVERRIDDEN": OVERRIDE, "EMPTY": ""}.items():
        shell.append('[[ "${GAZELLE_QA_' + k + '?}" == ' + shlex.quote(v) + ' ]]')
    shell.extend(['[[ "${!#}" == qa-marker ]]', "echo " + MARK])
    script = folder / "read_env.sh"
    script.write_text("\n".join(shell) + "\n")
    script.chmod(0o755)
    sample = folder / "sample"
    sample.mkdir()
    (sample / "probe.go").write_text("package sample\n")
    (sample / "BUILD.bazel").write_text("")


def supplier(arm, flavor):
    case = qa.REPORT["cases"][arm + "_" + flavor] = {"bazel": NATIVE, "rootMode": flavor}
    qa.save()
    row, out = proc(case, "runner", flavor, "run", "//bcr_tests/runner_qa:observe")
    row["verifiedOutput"] = out.decode(errors="replace").splitlines().count(MARK) == 1
    script = SUPPLIER / "bazel-bin/bcr_tests/runner_qa/observe-runner.bash"
    if row["exit"] == 0 and script.is_file():
        row["scriptSHA256"] = sha256(script.read_bytes()).hexdigest()
    row, out = proc(case, "actualGazelle", flavor, "run", "//bcr_tests/runner_qa:standard", timeout=850)
    row["generatedRule"] = b"go_library(" in out and b'"probe.go"' in out
    bep = qa.AREA / (arm + "-" + flavor + ".bep")
    row, _ = proc(case, "upstreamTests", flavor, "test", "--nocache_test_results",
                  "--build_event_json_file=" + str(bep), *["//tests/cli:" + s for s in TESTS])
    statuses = {}
    if bep.is_file():
        for line in bep.open():
            event = json.loads(line)
            label = event.get("id", {}).get("testResult", {}).get("label")
            if label and "testResult" in event and label.split(":")[-1] in TESTS:
                statuses.setdefault(label.split(":")[-1], []).append(event["testResult"].get("status"))
    row["statuses"] = statuses
    case["verified"] = (case["runner"]["exit"] == case["actualGazelle"]["exit"] == row["exit"] == 0 and
                        case["runner"]["verifiedOutput"] and case["actualGazelle"]["generatedRule"]
                        and statuses == {s: ["PASSED"] for s in TESTS})
    qa.save()


def patch_source(reverse=False):
    direction = ["--reverse"] if reverse else ["--forward"]
    subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", *direction, "-i", str(PATCH)], cwd=SUPPLIER, check=True)


def lazy_query(arm):
    case = qa.REPORT["cases"][arm + "_query_test"] = {}
    bep = qa.AREA / (arm + "-query.bep")
    runner_path = str(qa.AREA) + ":" + os.environ["PATH"]
    row, _ = proc(case, "test", "workspace", "test", "--strategy=TestRunner=local", "--test_output=errors",
                  "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_filter=^TestQueryWithoutGoEnv$",
                  "--nocache_test_results", "--test_timeout=360", "--test_env=PATH=" + runner_path,
                  "--test_env=USE_BAZEL_VERSION=" + NATIVE, "--build_event_json_file=" + str(bep),
                  "//internal:bazel_test", timeout=700)
    statuses = []
    if bep.is_file():
        for line in bep.open():
            e = json.loads(line)
            if e.get("id", {}).get("testResult", {}).get("label") == "//internal:bazel_test" and "testResult" in e:
                statuses.append(e["testResult"]["status"])
    log = SUPPLIER / "bazel-testlogs/internal/bazel_test/test.log"
    expected = "FAIL" if arm == "stock" else "PASS"
    if log.is_file() and statuses:
        data = log.read_bytes()
        row["testLogSHA256"] = sha256(data).hexdigest()
        row["namedOutcome"] = ("--- " + expected + ": TestQueryWithoutGoEnv ").encode() in data
        row["goEnvMissing"] = b"go_env.bzl" in data and b"bazel_gazelle_go_repository_config" in data
        diagnostics = []
        for line in data.decode(errors="replace").splitlines():
            if not re.search(r"(?i)(ERROR:|FATAL:|failed to|cannot |could not|not found|not recognized|unrecognized|exception|runner_test[.]go:)", line):
                continue
            line = line.replace(str(qa.AREA), "<fixture>").replace(str(qa.SRC), "<source>").replace("top-secret-token", "<fixture-value>")
            line = re.sub(r"(?i)(https?://)[^/\s]+@", r"\1<redacted>@", line)
            if re.search(r"(?i)(authorization|password|bearer|credential|token=|api[_-]?key=|secret=)", line):
                continue
            if line[:800] not in diagnostics:
                diagnostics.append(line[:800])
            if len(diagnostics) >= 20:
                break
        row["diagnostics"] = diagnostics
        row["actualPass"] = b"--- PASS: TestQueryWithoutGoEnv " in data
        row["actualFail"] = b"--- FAIL: TestQueryWithoutGoEnv " in data
    row["statuses"] = statuses
    case["verified"] = (row.get("namedOutcome", False) and statuses == [{"FAIL": "FAILED", "PASS": "PASSED"}[expected]] and
                        row["exit"] == (3 if arm == "stock" else 0) and (arm != "stock" or row.get("goEnvMissing")))
    qa.save()


def main():
    assert platform.machine() == "x86_64" and qa.git("rev-parse", "HEAD^") == "1b4ab0e4ced86345fe060569e8c3a491cdafa527"
    assert sha256(PATCH.read_bytes()).hexdigest() == PATCH_SHA
    qa.REPORT.update(supplierMaster=MASTER, archiveSHA256=SOURCE_SHA, patchSHA256=PATCH_SHA,
                     previousRun=42348501206, previousArtifact=13564587602)
    qa.save()
    SUPPLIER.mkdir()
    subprocess.run(["tar", "-xzf", str(qa.AREA / "supplier.tar.gz"), "--strip-components=1", "-C", str(SUPPLIER)], check=True)
    assert (SUPPLIER / ".bazelversion").read_text().strip() == NATIVE
    # The nested TestRunner clears HOME and XDG_CACHE_HOME. Invoke the Bazel
    # executable Bazelisk already resolved before entering the test environment.
    version = "bazel " + NATIVE
    env = {**os.environ, "USE_BAZEL_VERSION": NATIVE}
    assert subprocess.check_output([str(qa.AREA / "bazelisk"), "--version"], cwd=SUPPLIER, env=env, text=True).strip() == version
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    home = Path(os.environ.get("BAZELISK_HOME_LINUX") or os.environ.get("BAZELISK_HOME") or cache / "bazelisk")
    binary_sha = "7ff2b6a675b59a791d007c526977d5262ade8fa52efc8e0d1ff9e18859909fc0"
    binary = home / "downloads/sha256" / binary_sha / "bin/bazel"
    assert sha256(binary.read_bytes()).hexdigest() == binary_sha
    (qa.AREA / "bazel").symlink_to(binary)
    assert subprocess.check_output([str(qa.AREA / "bazel"), "--version"], cwd=SUPPLIER, env={"PATH": os.defpath}, text=True).strip() == version
    qa.REPORT["nestedBazelSHA256"] = binary_sha
    qa.save()
    regression = SUPPLIER / "internal/runner_test.go"
    old_test = regression.read_bytes()
    patch_source()
    new_test = regression.read_bytes()
    patch_source(reverse=True)
    setup_supplier()
    for arm in ("stock", "fixed"):
        if arm == "fixed":
            regression.write_bytes(old_test)
            patch_source()
        else:
            regression.write_bytes(new_test)
        lazy_query(arm)
    qa.REPORT["passed"] = all(qa.REPORT["cases"][a + "_query_test"]["verified"] for a in ("stock", "fixed"))
    qa.save()
    return int(not qa.REPORT["passed"])


if __name__ == "__main__":
    sys.exit(main())
