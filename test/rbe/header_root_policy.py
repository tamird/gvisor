"""Query only the existing kernel-header extension as a dependency and root."""

import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

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
            expected = re.search(r"tag class[^\n]*configure[^\n]*(?:does not exist|not found)", log, re.I)
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


if __name__ == "__main__":
    main()
