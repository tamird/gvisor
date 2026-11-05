"""Check the syscall launcher's public entry points and output directory contract."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import traceback
import uuid

import qualification_report as report

PRODUCT = "b0b2a33254ea8cc25320a078fba8d3e1852fad61"
CASES = {
    "//test/syscalls:fork_test_native": ["ForkTest.Simple", "ForkTest.Pipe", "ForkTest.SharedMapping"],
    "//test/syscalls:stat_times_test_native": ["StatTimesTest.FileRead"],
}
D = Path(os.environ["RUNNER_TEMP"])
OUTPUT = D / "out/verdict.json"
R = {"status": "INCOMPLETE", "source": PRODUCT, "carrier": os.environ["GITHUB_SHA"],
     "expected": CASES, "owners": {}, "errors": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    R["recorded_at"] = now()
    OUTPUT.write_text(json.dumps(R, sort_keys=True, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def error(exc):
    return {"type": type(exc).__name__, "lines": [fr.lineno for fr in traceback.extract_tb(exc.__traceback__)
            if Path(fr.filename).name == "syscall_launcher.py"]}


TERMS = ("No such file or directory", "Permission denied", "Operation not permitted", "Failed to fetch",
         "no such target", "No space left", "no matching toolchain", "no usable strategy", "fatal error:",
         "error:", "undefined reference", "atime setup requires", "enable TEST_TMPDIR atime", "creating undeclared outputs directory",
         "setting undeclared outputs permissions", "test must be provided", "flag provided but not defined")


def diagnostics(raw):
    lines = raw.decode(errors="replace").splitlines()
    fields = []
    for line in lines:
        match = re.search(r'(?:^|[ /])([A-Za-z0-9_./+-]+\.(?:cc|bzl|go)|BUILD(?:\.bazel)?):(\d+)(?::\d+)?: (?:fatal )?error: (.{1,160})', line)
        if match and len(fields) < 4 and not re.search(r'https?://|grpc|(?i:token|key|secret)|/home/|--|\w+=', match[3]):
            fields.append([Path(match[1]).name, int(match[2]), match[3]])
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "locations": fields,
            "classes": [s for s in TERMS if any(s.lower() in line.lower() for line in lines)]}


def check_owner(target, manifest, observations, completions):
    row = R["owners"][target] = {}
    test_id, obs = observations[target]
    strategy = obs.get("executionInfo", {})
    row.update(status=obs["status"], strategy=strategy.get("strategy"), cached=bool(
        obs.get("cachedLocally") or strategy.get("cachedRemotely")), config=test_id["configuration"]["id"])
    required_xml = frozenset(case.replace(".", "/", 1) for case in CASES[target])
    for name in ("test.log", "test.xml"):
        try:
            read_original(row, target, test_id, manifest, name, required_xml)
        except Exception as exc:
            row.setdefault("report_errors", {})[name] = error(exc)
    save()
    assert not row.get("report_errors") and (target, row["config"]) in completions
    assert row["status"] == "PASSED" and row["strategy"] == "local" and not row["cached"]
    assert row["runner_received_argument"] and row["positive_gtests"] == CASES[target]
    assert row["cases"] == sorted(required_xml) and row["passed"] == len(required_xml) and not row["failed"] and not row["skipped"]


def read_original(row, target, test_id, manifest, name, required_xml):
    item, = [f for f in manifest["reports"] if f["identity"] == test_id and f["name"] == name]
    raw = (D / item["path"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == item["sha256"] and len(raw) == item["bytes"]
    row[name] = {"bytes": len(raw), "sha256": digest}
    if name == "test.log":
        row["runner_received_argument"] = bool(re.search(rb"Host RLIMIT_NOFILE: soft=\d+ hard=\d+", raw))
        row["positive_gtests"] = [case for case in CASES[target]
                                if re.search(rb"\[\s*OK\s*\]\s+" + re.escape(case.encode()) + rb"(?=\s|$)", raw)]
        if row["status"] != "PASSED":
            row["diagnostic"] = diagnostics(raw)
    else:
        cases = report.xml_cases(D / item["path"], required_xml)
        row.update(cases=cases["required_passed"], passed=cases["passed"], failed=cases["failed"], skipped=cases["skipped"])


def mode_probe():
    # Exercise the very same native-test output the preceding Bazel invocation built.
    # A pre-existing sticky bit also distinguishes "+rwx" from replacing all permissions.
    launcher = Path("bazel-bin/test/syscalls/fork_test_native").resolve(strict=True)
    setup = Path("bazel-bin/test/runner/setup_container/setup_container").resolve(strict=True)
    assert all(p.is_relative_to(Path("bazel-out").resolve()) for p in (launcher, setup))
    data = launcher.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62 and data == setup.read_bytes()
    result = R["output_directory"] = {"launcher_sha256": hashlib.sha256(data).hexdigest(), "observations": {}}
    for initial, want, mask in (("absent-022", 0o777, 0o022), ("absent-027", 0o777, 0o027),
                                ("sticky-setgid", 0o3777, 0o022), ("file", 0o640, 0o022)):
        path = D / ("directory-" + initial) / "child"
        if initial == "sticky-setgid":
            path.mkdir(parents=True)
            path.chmod(0o3700)
        elif initial == "file":
            path.parent.mkdir()
            path.touch()
            path.chmod(want)
        else:
            assert not path.parent.exists()
        cmd = subprocess.run([launcher, "--syscall-test", "/usr/bin/stat", path, "--format=%a"],
                             env={"TEST_UNDECLARED_OUTPUTS_DIR": str(path), "LC_ALL": "C"},
                             capture_output=True, umask=mask, timeout=10)
        seen = stat.S_IMODE(path.stat().st_mode)
        obs = result["observations"][initial] = {"exit": cmd.returncode, "actual_mode": seen, "expected_mode": want}
        if initial == "file":
            obs["regular_file_rejected"] = b"Not a directory" in cmd.stderr and not cmd.stdout
            save()
            assert path.is_file() and seen == want and cmd.returncode == 1 and obs["regular_file_rejected"]
        else:
            obs["runner_observed_mode"] = cmd.stdout.strip() == format(want, "o").encode()
            if initial.startswith("absent"):
                obs["parent_mode"] = stat.S_IMODE(path.parent.stat().st_mode)
            save()
            assert path.is_dir() and seen == want and cmd.returncode == 0 and obs["runner_observed_mode"]
            if initial.startswith("absent"):
                assert obs["parent_mode"] == 0o777 & ~mask


def main():
    save()
    assert platform.machine() == "x86_64" and git("rev-parse", "HEAD") == PRODUCT
    assert git("rev-parse", "HEAD^{tree}") == "cee924ee16f0a8addfef20295fd3138a8a15c84f"
    caps = int(re.search(r"^CapEff:\s*([0-9a-f]+)", Path("/proc/self/status").read_text(), re.M)[1], 16)
    assert not caps & ((1 << 12) | (1 << 21))  # No NET_ADMIN or SYS_ADMIN.
    R["admin_capabilities"] = False
    checks = ["clang-format", "cpplint", "buildifier"]
    lint = subprocess.run(["tools/lint.sh", *checks], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    checks_run = re.findall(r"^  PASS  (\S+)$", lint.stdout.decode(errors="replace"), re.MULTILINE)
    R["lint"] = {"exit": lint.returncode, "checks": checks_run, "diagnostic": diagnostics(lint.stdout)}
    save()
    assert lint.returncode == 0 and checks_run == checks
    invocation = str(uuid.uuid4())
    args = ["test", "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
            "--remote_cache=", "--remote_executor=", "--bes_backend=", "--keep_going",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
            "--test_sharding_strategy=disabled", "--test_timeout=180", "--test_output=errors",
            "--test_arg=--host-nofile=1", "--test_env=GTEST_FILTER=" + ":".join(case for seq in CASES.values() for case in seq),
            "--build_event_json_file_path_conversion=false", "--invocation_id=" + invocation,
            "--build_event_json_file=" + str(D / "bep"), *CASES]
    with (D / "console").open("wb") as console:
        proc = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "2700", "bazel", "--batch",
                               "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", *args], stdout=console, stderr=subprocess.STDOUT)
    R.update(native_exit=proc.returncode, finished=now(), requested=invocation, diagnostic=diagnostics((D / "console").read_bytes()))
    save()
    manifest = report.capture(D / "bep", D / "events", Path("bazel-out"), D / "reports", retain_outputs=True, expected_kind="test")
    R["capture_errors"] = len(manifest["errors"])
    results, completed = {}, set()
    for line in (D / "events").open():
        e = json.loads(line)
        if "started" in e:
            R["observed"] = e["started"]["uuid"]
        if "finished" in e:
            R["bep_exit"] = e["finished"]["exitCode"].get("code", 0)
        if "completed" in e and e["completed"].get("success"):
            t = e["id"]["targetCompleted"]
            completed.add((t["label"], t["configuration"]["id"]))
        if "testResult" in e:
            t = e["id"]["testResult"]
            assert t["label"] not in results and all(t.get(k, 1) == 1 for k in ("attempt", "run", "shard"))
            results[t["label"]] = (t, e["testResult"])
    for target in CASES:
        try:
            check_owner(target, manifest, results, completed)
        except Exception as exc:
            R["errors"][target] = error(exc)
    try:
        mode_probe()
    except Exception as exc:
        R["errors"]["directory"] = error(exc)
    assert not R["errors"] and set(results) == set(CASES) and R["observed"] == invocation
    assert not R["capture_errors"] and R["native_exit"] == R["bep_exit"] == 0
    assert not git("status", "--porcelain", "--untracked-files=no")
    R["status"] = "PASS"


try:
    main()
except Exception as exc:
    R["errors"]["overall"] = error(exc)
    if R.get("native_exit") not in (None, 0, 124, 137):
        R["status"] = "FAIL"
finally:
    save()
raise SystemExit(R["status"] != "PASS")
