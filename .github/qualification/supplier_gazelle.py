"""Exercise the supplier runner and an otherwise unchanged external gVisor module."""

from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys

import downstream_llvm as qa


GVISOR = "69e89d7a3dfc6b2b484f7dc01a68fad68741dd03"
RELEASE = "b66b19ce4b0ecebd3ae416e6b55a0d22a49fef34"
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
           op, *modes, "--repo_env=GAZELLE_QA_INHERITED", "--jobs=3", "--lockfile_mode=off", *args]
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


def downstream():
    c = qa.REPORT["cases"]["external"] = {"source": GVISOR}
    qa.CONSUMER.mkdir()
    module = ('module(name = "downstream")\nbazel_dep(name = "gvisor", version = "0.0.1")\n'
              f'local_path_override(module_name = "gvisor", path = {json.dumps(str(qa.SRC))})\n'
              'single_version_override(module_name = "gazelle", version = "0.51.3", '
              'patches = ["//:supplier.patch"], patch_strip = 1)\n')
    (qa.CONSUMER / "MODULE.bazel").write_text(module)
    (qa.CONSUMER / ".bazelversion").write_text("9.3.1\n")
    (qa.CONSUMER / "BUILD.bazel").write_text('exports_files(["supplier.patch"])\n')
    (qa.CONSUMER / "supplier.patch").write_bytes(PATCH.read_bytes())
    api = qa.CONSUMER / "old_api"
    api.mkdir()
    (api / "BUILD.bazel").write_text('load("@gvisor//tools/bazeldefs:go.bzl", "gazelle")\nfilegroup(name = "marker")\n')
    c["consumerModule"] = module.replace(str(qa.SRC), "<source>")
    selected = "set(@gvisor//:license @gvisor//:sandbox //old_api:marker)"
    row, out = qa.bazel(c, "metadataAndAPI", "query", selected, "--output=label")
    row["labels"] = sorted(s for s in out.decode(errors="replace").splitlines() if s.startswith(("@", "//")))
    qa.plan(c)
    qa.build(c)
    c["sameKnownELF"] = c["build"].get("elf", {}).get("sha256") == "7e34fe3c975a09c00f3486e7c9a447493b142e7aa02f989e4222d6cb39e7077a"
    qa.save()
    subprocess.run([str(qa.AREA / "bazelisk"), "--nosystem_rc", "--nohome_rc", "--noworkspace_rc", "shutdown"],
                   cwd=qa.CONSUMER, capture_output=True, timeout=90)


def setup_supplier():
    SUPPLIER.mkdir()
    subprocess.run(["tar", "-xzf", str(qa.AREA / "supplier.tar.gz"), "-C", str(SUPPLIER)], check=True)
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


def main():
    assert platform.machine() == "x86_64" and qa.git("rev-parse", "HEAD^^") == GVISOR
    assert sha256(PATCH.read_bytes()).hexdigest() == "44496c0571f5bd7c2418edb9d77d5fa407a60ced47a3dfa28f49ed6d727adc7a"
    qa.git("switch", "--quiet", "--detach", GVISOR)
    qa.REPORT.update(supplierRelease=RELEASE, archiveSHA256="49d9eba309b0b695824ff417d734242824ad9ab5edb56063b9d3400df1a61a56",
                     patchSHA256=sha256(PATCH.read_bytes()).hexdigest())
    qa.save()
    try:
        downstream()
    except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
        qa.REPORT["cases"]["external"]["collectionError"] = type(exc).__name__
        qa.save()
    setup_supplier()
    for arm in ("stock", "fixed"):
        if arm == "fixed":
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(PATCH)], cwd=SUPPLIER, check=True)
        for flavor in ("bzlmod", "workspace"):
            try:
                supplier(arm, flavor)
            except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
                qa.REPORT["cases"][arm + "_" + flavor]["collectionError"] = type(exc).__name__
            qa.save()
    cases = qa.REPORT["cases"]
    external = cases["external"]
    qa.REPORT["runnerScriptsIdentical"] = all(cases["stock_" + f]["runner"].get("scriptSHA256") and
        cases["stock_" + f]["runner"]["scriptSHA256"] == cases["fixed_" + f]["runner"].get("scriptSHA256")
        for f in ("bzlmod", "workspace"))
    qa.REPORT["passed"] = (external.get("linkedARM64", False) and external["sameKnownELF"]
                           and len(external["metadataAndAPI"]["labels"]) == 3
                           and external["metadataAndAPI"]["exit"] == 0
                           and qa.REPORT["runnerScriptsIdentical"]
                           and all(cases[a + "_" + f].get("verified", False) for a in ("stock", "fixed") for f in ("bzlmod", "workspace")))
    qa.save()
    return int(not qa.REPORT["passed"])


if __name__ == "__main__":
    sys.exit(main())
