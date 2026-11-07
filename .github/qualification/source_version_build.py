"""Build the exported tree, then restore real guests from both installations."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(os.environ["RUNNER_TEMP"], "source-version")
SOURCE = ROOT / "source"
BAZEL = [str(ROOT / "bazel"), "--nohome_rc", "--nosystem_rc"]


def run(args, path):
    with path.open("w") as out:
        return subprocess.run(args, cwd=SOURCE, stdout=out, stderr=subprocess.STDOUT).returncode


def check(name, label, version):
    dest = ROOT / name
    dest.mkdir()
    flags = ["--config=x86_64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--noshow_progress", "--color=no"]
    if name == "release":
        flags.extend(["--config=release_version", "--embed_label=" + label])
    # The test and the release consume exactly this source tree with no VCS data.
    row = {"name": name, "expectedLabel": label, "passed": False}
    try:
        tests = dest / "test.log"
        row["testExit"] = run([*BAZEL, "test", *flags, "--nocache_test_results", "--test_output=all",
                               "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "//runsc:version_test"], tests)
        row["namedCase"] = "--- PASS: TestVersion " in tests.read_text(errors="replace")
        build = dest / "build.log"
        row["buildExit"] = run([*BAZEL, "build", *flags, "//:release", "//tools/bazeldefs:version"], build)
        assert row["buildExit"] == 0
        row["packageVersion"] = (SOURCE / "bazel-bin/tools/bazeldefs/version.txt").read_text().strip()
        assert row["packageVersion"] == version
        shutil.copytree(SOURCE / "bazel-bin/release", dest / "packaged")
        shutil.copy2(ROOT / "guest-busybox", dest / "guest-busybox")
        row["guestExit"] = run(["sudo", "-n", "env", "-i", "HOME=/root", "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                                "RELEASE_DIR=" + str(dest), "RELEASE_LABEL=" + label,
                                "/usr/bin/python3", "-IB", str(ROOT / "guest.py")], dest / "guest.log")
        row["parserBuildExit"] = run([*BAZEL, "build", *flags, "//tools/parsers:parser"], dest / "parser.log")
        assert row["guestExit"] == 0
        guest = json.loads((dest / "runtime.json").read_text())
        row["passed"] = (guest["restored_guest"] and guest["removed_runtime_root"] and len(guest["file_sha256"]) == 9
                         and row["testExit"] == 0 and row["namedCase"] and row["parserBuildExit"] == 0)
        assert row["passed"]
    except (OSError, AssertionError, KeyError, ValueError) as exc:
        row["phaseError"] = type(exc).__name__
        for log in dest.glob("*.log"):
            text = log.read_text(errors="replace")
            row[log.name + "Diagnostics"] = [l[:300] for l in text.splitlines() if l.startswith(("ERROR:", "AssertionError", "Traceback", "FATAL:", "OSError", "PermissionError", "FileNotFoundError", "RuntimeError", "subprocess.CalledProcessError"))][-5:]
            row[log.name + "SHA256"] = hashlib.sha256(log.read_bytes()).hexdigest()
    finally:
        (dest / "result.json").write_text(json.dumps(row, indent=2) + "\n")
    return row["passed"]


if __name__ == "__main__":
    assert not (SOURCE / ".git").exists()
    assert subprocess.run(["git", "-C", str(SOURCE), "rev-parse", "--git-dir"], capture_output=True).returncode
    assert not (SOURCE / "tools/workspace_status.sh").exists()
    results = [check("development", "(devel)", "0~devel"), check("release", "release-20261106.0", "20261106.0")]
    raise SystemExit(0 if all(results) else 1)
