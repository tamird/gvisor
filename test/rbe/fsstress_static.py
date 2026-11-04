"""Resolve the fsstress toolchain load from a consuming Bazel module."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess

import continuous

PRODUCT = "588e6f3436b08e9b9a49e656458d9ea6178bfedd"
FIXED = "50c1f8f6a141d85f97bcd4c6a70bd16c2c68f60c"
CONTROL = "e38d2eff646dc205441dcaf705fbb4b8d047e929"
FILE = "tools/bazeldefs/static.bzl"
FILE_BLOB = "323520254911adc6b5ff29213cfc6dd3aac0cf54"
MODULE_BLOB = "9fcacce4372009768064930009b125632167a7d0"
POLICY_ERROR = ('Using the "go_deps.gazelle_override" tag in a non-root Bazel module is forbidden, '
                'but module "gvisor" requests it.')
TAG_SHA = "59a3c7342f5adfe1a62503cb8c19bc1fbd54e4373081b2777e11182fc378a85a"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "fsstress-static"
continuous.ROOT = ROOT
R = {"status": "INCOMPLETE", "product": PRODUCT, "policy_tag_sha256": TAG_SHA,
     "consumer_supplied_gazelle_overrides": 0,
     "explicit_consumer_prior_artifact": 13343998937,
     "expected": ["stock", "module_relative", "root_relative"], "phases": {},
     "archive_and_original_query": {"artifact": 13343778034,
        "sha256": "44f3dd76fc9fbbb0f88432fa55f81d12ce5e3dca023caef8135cdd574766eb41",
        "amd64_image_and_consumer": "PASS", "arm64_image": "PASS", "arm64_runtime_executed": False,
        "original_downstream_exit": 7, "original_gazelle_location": "go_deps.bzl:101:13"},
     "policy_proof": {"artifact": 13120208022,
        "sha256": "76b231f008583f2bfa077a7a79bf32f21139f2b09079ed5775ea598dd789e61e",
        "tested_policy": "26ef225766ba82a92fefbbc9795c3d697605489b"}}

def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    R["verified_at"] = now()
    R["missing"] = [name for name in R["expected"] if R["phases"].get(name, {}).get("status") != "PASS"]
    (ROOT / "out/verdict.json").write_text(json.dumps(R, sort_keys=True, indent=2) + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def downstream(name, source, directory):
    row = R["phases"][name] = {"status": "INCOMPLETE", "source": source, "started_at": now()}
    save()
    subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
    assert continuous.git("rev-parse", "HEAD") == source
    row["selected_file_blob"] = continuous.git("rev-parse", "HEAD:" + FILE)
    assert row["selected_file_blob"] == FILE_BLOB
    # Restrict the loads to the real source of the image's binary filegroup.
    expression = ("loadfiles(labels(srcs, @kernel_dep//images/basic:fsstress_binary))"
                  " intersect @kernel_dep//tools/bazeldefs:static.bzl")
    result = subprocess.run(
        ["timeout", "-k", "10", "180", os.environ["REAL_BAZEL"], "--batch", "--ignore_all_rc_files",
         "--host_jvm_args=-Xmx2g", "query", "--output=label", expression],
        cwd=directory, capture_output=True,
    )
    row.update(native_exit=result.returncode, finished_at=now(), query=expression,
               stdout_sha256=sha(result.stdout), stderr_sha256=sha(result.stderr))
    label_pattern = r"@{1,2}[a-zA-Z0-9_.+-]+//tools/bazeldefs:static[.]bzl"
    lines = result.stdout.decode(errors="replace").splitlines()
    row["selected_labels"] = [line for line in lines if re.fullmatch(label_pattern, line)]
    diagnostics = result.stderr.decode(errors="replace").splitlines()
    row["source_trace"] = [
        match.group(0) for line in diagnostics
        for match in re.finditer(r"(?:go_deps|go_env|static|defs|img|pkg)[.]bzl:[0-9]+:[0-9]+", line)
    ][:10]
    # A provider or dependency failure cannot establish the root-relative control.
    missing = [line for line in diagnostics if "tools/bazeldefs" in line and
               re.search(r"no such package|does not have one|every [.]bzl file|unable to find|"
                         r"build file not found|cannot load|not found", line, re.I)]
    row["missing_root_load"] = [continuous.diagnostic(line) for line in missing
                                if re.search(r"(?<![A-Za-z0-9_+.-])@{0,2}//tools/bazeldefs(?:[:'/]|$)", line)][:2]
    row["gazelle_error"] = next((POLICY_ERROR for line in diagnostics if POLICY_ERROR in line), None)
    row["gazelle_source"] = next(("go_deps.bzl:101:13" for line in diagnostics if "go_deps.bzl:101:13" in line), None)
    if name == "stock":
        passed = (result.returncode == 7 and not lines and
                  bool(row["gazelle_error"]) and bool(row["gazelle_source"]))
    elif name == "root_relative":
        passed = result.returncode != 0 and not row["selected_labels"] and any(
            re.search(r"(?<![A-Za-z0-9_+.-])@{0,2}//tools/bazeldefs(?:[:'/]|$)", line)
            for line in missing)
    else:
        passed = result.returncode == 0 and lines == ["@kernel_dep//tools/bazeldefs:static.bzl"]
    row["status"] = "PASS" if passed else "FAIL"
    save()


def main():
    save()
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    assert continuous.git("rev-parse", CONTROL + "^") == FIXED
    assert continuous.git("rev-parse", FIXED + "^") == PRODUCT
    assert set(continuous.git("diff", "--name-only", PRODUCT, FIXED).splitlines()) == {
        "images/img.bzl", "tools/BUILD", "tools/bazeldefs/BUILD", "tools/defs.bzl", "tools/pkg.bzl"
    }
    assert continuous.git("diff", "--name-only", FIXED, CONTROL) == "images/basic/ltp.BUILD"
    for source, blob in [(CONTROL, "986350b252b05ff4b48af2ffb758f854f39b3f0d"),
                         (FIXED, "df342d1ddc1bde2bb7aad17854ecca4f3a50b6c1")]:
        assert continuous.git("rev-parse", source + ":images/basic/ltp.BUILD") == blob
        assert continuous.git("rev-parse", source + ":MODULE.bazel") == MODULE_BLOB
    assert continuous.git("rev-parse", PRODUCT + ":MODULE.bazel") == MODULE_BLOB
    assert subprocess.check_output([os.environ["REAL_BAZEL"], "--version"], text=True).strip() == "bazel 9.3.1"
    module = continuous.git("show", PRODUCT + ":MODULE.bazel")
    tags = module[module.index("go_deps.module_override("):] + "\n"
    assert sha(tags.encode()) == TAG_SHA
    assert tags.count("go_deps.module_override(") == 1 and tags.count("go_deps.gazelle_override(") == 9
    directory = ROOT / "downstream"
    directory.mkdir()
    manifest = ('module(name = "fsstress_consumer")\n'
                'bazel_dep(name = "gvisor", version = "0.0.1", repo_name = "kernel_dep")\n'
                f'local_path_override(module_name = "gvisor", path = {json.dumps(str(Path.cwd()))})\n')
    (directory / "MODULE.bazel").write_text(manifest)
    R["consumer_manifest_sha256"] = continuous.digest(directory / "MODULE.bazel")
    R["gvisor_module_blob"] = MODULE_BLOB
    for name, source in (("stock", PRODUCT), ("module_relative", FIXED), ("root_relative", CONTROL)):
        downstream(name, source, directory)
    R["status"] = "PASS" if not R["missing"] else "FAIL"
    save()
    return R["status"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
