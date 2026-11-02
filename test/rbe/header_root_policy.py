"""Query only the existing kernel-header extension as a dependency and root."""

import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from external_go_policy import details

BASE = "aa767e4c43d011121271551fe24da501a56f729d"
PRODUCT = "48428434392bfa3b1a1ce9fb50ca404b38f21b73"
AREA = Path(os.environ["RUNNER_TEMP"]) / "header-policy"
SOURCE = Path.cwd()
REPORT = {"verdict": "INCOMPLETE", "base": BASE, "product": PRODUCT, "queries": {}}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    (AREA.parent / "header-policy.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def missing_configure(log):
    return "does not have a tag class named configure" in log and "kernel_headers.bzl" in log


def query(name, source, work, target):
    row = {"source": source, "target": target, "status": "INCOMPLETE"}
    REPORT["queries"][name] = row
    try:
        subprocess.run(["git", "checkout", "--detach", source], check=True)
        assert git("rev-parse", "HEAD") == source
        assert not git("status", "--porcelain", "--untracked-files=no")
        row["module_sha256"] = sha256((SOURCE / "MODULE.bazel").read_bytes()).hexdigest()
        row["backport_sha256"] = sha256((SOURCE / "tools/llvm-linux-header-default.patch").read_bytes()).hexdigest()
        row["consumer_module_sha256"] = sha256((work / "MODULE.bazel").read_bytes()).hexdigest()
        command = [str(AREA / "bazel-8.8.1"), "--batch", "--nosystem_rc", "--nohome_rc",
                   "--noworkspace_rc", "query", target, "--output=xml", "--lockfile_mode=off"]
        row["command"] = command
        row["started"] = datetime.now(timezone.utc).isoformat()
        result = subprocess.run(command, cwd=work, capture_output=True, timeout=600)
        row.update(exit=result.returncode, finished=datetime.now(timezone.utc).isoformat(),
                   stdout_sha256=sha256(result.stdout).hexdigest(), stderr_sha256=sha256(result.stderr).hexdigest())
        log = result.stderr.decode(errors="replace")
        row["diagnostics"] = details(log)
        assert not git("status", "--porcelain", "--untracked-files=no")
        if name == "before_dependency":
            expected = missing_configure(log)
            row["expected_rejection"] = bool(expected)
            row["status"] = "PASS" if result.returncode != 0 and expected and "kernel_headers" in log else "FAIL"
            assert any("configure" in line for line in row["diagnostics"])
            return
        if result.returncode:
            row["status"] = "FAIL"
            return
        files = ET.fromstring(result.stdout).findall("source-file")
        assert len(files) == 1
        node = files[0]
        assert node.attrib["name"].endswith("//:linux_kernel_version_map.bzl")
        path = Path(node.attrib["location"].rsplit(":", 2)[0])
        assert path.name == "linux_kernel_version_map.bzl"
        data = path.read_bytes()
        values = {n.targets[0].id: ast.literal_eval(n.value) for n in ast.parse(data).body
                  if isinstance(n, ast.Assign)}
        row.update(generated_label=node.attrib["name"], generated_sha256=sha256(data).hexdigest(),
                   default=values.get("DEFAULT_LINUX_KERNEL_VERSION"),
                   indexed_6_18=values["LINUX_KERNEL_VERSION_MAP"]["6.18"])
        assert row["indexed_6_18"].startswith("6.18.")
        assert row["default"] == ("6.18" if name == "after_root" else None)
        row["status"] = "PASS"
    except Exception as exc:
        row["collection_error"] = type(exc).__name__
        if row["status"] != "FAIL":
            row["status"] = "INCOMPLETE"
    finally:
        save()


def main():
    assert git("rev-parse", PRODUCT + "^") == BASE
    work = AREA / "consumer"
    work.mkdir()
    (work / "MODULE.bazel").write_text(
        'module(name="consumer")\nbazel_dep(name="gvisor",version="0.0.1")\n'
        f'local_path_override(module_name="gvisor",path={json.dumps(str(SOURCE))})\n'
        'bazel_dep(name="llvm",version="0.8.24")\n'
        'headers=use_extension("@llvm//kernel/extension:kernel_headers.bzl","kernel_headers")\n'
        'use_repo(headers,probe_headers="kernel_headers")\n')
    (work / "BUILD.bazel").write_text("")
    query("before_dependency", BASE, work, "@probe_headers//:linux_kernel_version_map.bzl")
    query("after_dependency", PRODUCT, work, "@probe_headers//:linux_kernel_version_map.bzl")
    query("after_root", PRODUCT, SOURCE, "@kernel_headers//:linux_kernel_version_map.bzl")
    rows = REPORT["queries"]
    assert rows["before_dependency"]["consumer_module_sha256"] == rows["after_dependency"]["consumer_module_sha256"]
    assert len({row["backport_sha256"] for row in rows.values()}) == 1
    REPORT["verdict"] = "PASS" if all(row["status"] == "PASS" for row in rows.values()) else "FAIL"
    REPORT["finished"] = datetime.now(timezone.utc).isoformat()
    save()
    raise SystemExit(REPORT["verdict"] != "PASS")


def reconcile(path):
    import check

    assert "LINT_RC" not in os.environ
    assert details("ERROR: linux/vfio.h: header not found") == ["ERROR: linux/vfio.h: header not found"]
    try:
        check.invoke("configuration_guard", "build", "//:unused")
    except KeyError as exc:
        assert exc.args == ("LINT_RC",)
    else:
        raise AssertionError("lint invocation accepted missing configuration")
    assert not check.OUT["phases"]
    REPORT["reporter_import"] = {"status": "PASS", "lint_rc_absent": True, "invoke_requires_lint_rc": True,
                                 "helper": "56ed4a613a3f82c5005718e4404a5393647aba06"}
    save()
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    assert digest == "e3d1608cca1e6de3176abada6ffd65faf8d1e90a4d00d05a5aebd8152ac439e5"
    with ZipFile(path) as archive:
        assert archive.namelist() == ["header-policy.json"]
        original = json.loads(archive.read("header-policy.json"))
    REPORT["original"] = original
    REPORT["original_archive_sha256"] = digest
    save()
    assert original["base"] == BASE and original["product"] == PRODUCT and original["verdict"] == "FAIL"
    rows = original["queries"]
    assert set(rows) == {"before_dependency", "after_dependency", "after_root"}
    before = rows["before_dependency"]
    assert before["source"] == BASE and before["exit"] == 7 and before["status"] == "FAIL"
    assert details("\n".join(before["diagnostics"])) == before["diagnostics"]
    assert missing_configure("\n".join(before["diagnostics"]))
    REPORT["retained_error_roundtrip"] = True
    assert before["consumer_module_sha256"] == rows["after_dependency"]["consumer_module_sha256"]
    assert len({r["backport_sha256"] for r in rows.values()}) == 1
    for name, default in [("after_dependency", None), ("after_root", "6.18")]:
        row = rows[name]
        assert row["status"] == "PASS" and row["source"] == PRODUCT and row["exit"] == 0
        assert row["default"] == default and row["indexed_6_18"] == "6.18.33"
    delivery = "d85bc40fda9451667fb01002c577d62ed8308b59"
    tree = git("rev-parse", PRODUCT + "^{tree}")
    assert git("rev-parse", delivery + "^{tree}") == tree
    REPORT.update(verdict="PASS", delivery_source=delivery, product_tree=tree,
                  reconciled_queries={name: "PASS" for name in rows}, new_queries=0,
                  finished=datetime.now(timezone.utc).isoformat())
    save()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--reconcile":
        reconcile(Path(sys.argv[2]))
    else:
        assert len(sys.argv) == 1
        main()
