"""Run the unchanged public C++ rule under the three supported Bazel majors."""

import json
import os
from pathlib import Path
import re
import struct
import subprocess

native = Path(__file__).resolve().parent
out = Path(os.environ["RUNNER_TEMP"]) / "owners"
versions = ("7.7.2", "8.8.2", "9.3.1")
owners = ("ordinary_test", "configured_test")
labels = ["//:" + name + suffix for name in owners for suffix in ("", ".dwp", ".stripped")]
result = {}

def bazel(version, cmd, args):
    argv = ["bazelisk", "--nosystem_rc", "--nohome_rc", "--noworkspace_rc", cmd, *args]
    p = subprocess.run(argv, cwd=native, env=dict(os.environ, USE_BAZEL_VERSION=version),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=780)
    return p.returncode, p.stdout

for version in versions:
    row = result[version] = {"tests": {}, "outputs": {}, "verified": False}
    try:
        rc, text = bazel(version, "version", ["--gnu_format"])
        assert rc == 0 and ("bazel " + version) in text
        base = ["--enable_bzlmod", "--lockfile_mode=off", "--jobs=4", "--color=no", "--curses=no",
                "--@rules_cc//:link_extra_libs=//:link_hook"]
        rc, text = bazel(version, "test", [*base, "--nocache_test_results", "--test_output=all", *labels])
        row["test_exit"] = rc
        for name, cfg in zip(owners, (0, 1)):
            log = native / "bazel-testlogs" / name / "test.log"
            row["tests"][name] = log.is_file() and f"CC_CONTRACT x86_64 707 {cfg}" in log.read_text()
        # The output aliases for a transitioned target need not share the
        # untransitioned rule's output directory.
        output_labels = [label for label in labels if label.endswith((".stripped", ".dwp"))]
        query_rc, query = bazel(version, "cquery", [*base, "--output=files", "set(" + " ".join(output_labels) + ")"])
        row["output_query_exit"] = query_rc
        for line in query.splitlines():
            if not re.fullmatch(r"bazel-out/[\w./+=,~-]+\.(?:dwp|stripped)", line):
                continue
            p = native / line
            if not p.is_file():
                continue
            if p.suffix == ".stripped":
                with p.open("rb") as f:
                    h = f.read(20)
                if h[:4] != b"\x7fELF" or struct.unpack_from("<H", h, 18)[0] != 62:
                    continue
            row["outputs"][line] = p.stat().st_size
        rc_kind, kind = bazel(version, "query", ["--enable_bzlmod", "--lockfile_mode=off", "--output=label_kind", "//:ordinary_test"])
        row["ordinary_name"] = rc_kind == 0 and "cc_test rule //:ordinary_test" in kind
        row["verified"] = (rc == 0 and query_rc == 0 and all(row["tests"].values()) and row["ordinary_name"] and
                           sum(k.endswith(".dwp") for k in row["outputs"]) == 2 and
                           sum(k.endswith(".stripped") for k in row["outputs"]) == 2)
        if not row["verified"]:
            row["errors"] = [term for term in ("no such package", "no such target", "failed to fetch",
                              "no matching toolchain", "unknown attribute", "undefined reference",
                              "out of memory", "no space left", "cannot be extended")
                             if term in (text + query + kind).lower()]
    except (OSError, AssertionError, subprocess.TimeoutExpired) as e:
        row["failure"] = type(e).__name__
    (out / "compatibility.json").write_text(json.dumps(result, sort_keys=True) + "\n")
    # The following compatibility lane selects a different Bazel server.
    try:
        bazel(version, "shutdown", [])
    except (OSError, subprocess.TimeoutExpired):
        pass
