"""Qualify external buffer restoration and compare three native ReadAt arms."""

from __future__ import annotations

from collections import Counter
import datetime
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET

import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "results"
BASE = "7bffefa9185aa55ba377e8a5534210a7f0cbf266"
PUBLIC = "5af539b23594b3711fd49c514e236442baad9df3"
REGRESSION = "296615c83a03899059e1e37f9bfc34fd68efdf71"
PRODUCT = os.environ["PRODUCT"]
LABEL = "//pkg/buffer:buffer_test"
BUILD = "pkg/buffer/BUILD"
TEST = "pkg/buffer/buffer_test.go"
BENCH = "pkg/buffer/read_qualification_test.go"
RESTORE_CASES = {"TestExternalStorageRestoreOrder/Buffer", "TestExternalStorageRestoreOrder/View"}
FAILURE = "storage releases = 1, loaded before release = false; want one release after load"
BENCH_NAMES = {"BenchmarkReadAt", "BenchmarkReadAtMultiView", "BenchmarkViewListAsSlice"}
BENCH_ROW = re.compile(r"^(BenchmarkReadAt(?:MultiView)?|BenchmarkViewListAsSlice)(?:-1)?\s+(\d+)\s+([0-9.]+) ns/op\s+(\d+) B/op\s+(\d+) allocs/op\s*$")
PACKAGE_FILES = ["pkg/buffer/" + name for name in ("BUILD", "buffer.go", "buffer_state.go", "buffer_test.go", "chunk.go", "view.go", "view_test.go", "view_unsafe.go")]
FORBIDDEN = re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|BEGIN (RSA |OPENSSH )?PRIVATE KEY|(?i:authorization|x-buildbuddy-api-key)\s*[:=]")
DEADLINE = time.monotonic() + 100 * 60


def utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def source_bytes(source: str, path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{source}:{path}"])


def save(info: dict) -> None:
    data = (json.dumps(info, sort_keys=True, indent=2) + "\n").encode()
    assert len(data) < 1024**2 and not FORBIDDEN.search(data), "Unsafe or oversized receipt"
    (OUT / "verdict.json").write_bytes(data)


def checkout(source: str, package_source: str | None = None) -> dict[str, str]:
    # Only the disposable remote checkout is changed; no user checkout is used.
    git("restore", "--worktree", "--", *PACKAGE_FILES)
    Path(BENCH).unlink(missing_ok=True)
    git("checkout", "--quiet", "--detach", source)
    assert git("rev-parse", "HEAD") == source, "Wrong source checkout"
    if package_source is not None:
        assert source == PRODUCT, "Benchmark roots must be identical"
        for path in PACKAGE_FILES:
            Path(path).write_bytes(source_bytes(package_source, path))
        build = Path(BUILD).read_text()
        assert build.count('        "buffer_test.go",') == 1, "Ambiguous benchmark owner"
        assert build.count('    name = "buffer",') == 1, "Ambiguous library for inline remarks"
        build = build.replace('    name = "buffer",', '    name = "buffer",\n    gc_goopts = ["-m=2"],')
        assert build.count('    name = "buffer_test",') == 1
        build = build.replace('    name = "buffer_test",', '    name = "buffer_test",\n    gc_goopts = ["-m=2"],')
        Path(BUILD).write_text(build.replace('        "buffer_test.go",', '        "buffer_test.go",\n        "read_qualification_test.go",'))
        shutil.copyfile(TMP / "buffer_read_benchmark.go", BENCH)
        changed = set(git("diff", "--name-only", "HEAD").splitlines())
        expected = set(git("diff", "--name-only", PRODUCT, package_source, "--", "pkg/buffer").splitlines()) | {BUILD}
        assert changed == expected, "Unexpected benchmark source delta"
        for path in set(PACKAGE_FILES) - {BUILD}:
            assert Path(path).read_bytes() == source_bytes(package_source, path), "Benchmark package source changed"
    else:
        assert not git("diff", "--name-only", "HEAD"), "Unexpected tracked source delta"
    return {path: git("hash-object", path) for path in PACKAGE_FILES}


def finish_time(payload: dict) -> str:
    if "testAttemptStart" in payload and "testAttemptDuration" in payload:
        start = datetime.datetime.fromisoformat(payload["testAttemptStart"].replace("Z", "+00:00"))
        seconds = float(payload["testAttemptDuration"].removesuffix("s"))
    else:
        start = datetime.datetime.fromtimestamp(int(payload["testAttemptStartMillis"]) / 1000, datetime.timezone.utc)
        seconds = int(payload["testAttemptDurationMillis"]) / 1000
    return (start + datetime.timedelta(seconds=seconds)).isoformat()


def cases(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.get("name")
        assert name and name not in rows, "Missing or duplicate XML testcase"
        if case.find("failure") is not None or case.find("error") is not None:
            status = "FAILED"
        elif case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed":
            status = "SKIPPED"
        else:
            status = "PASSED"
        rows[name] = status
    assert rows, "No XML testcases"
    return rows


def invoke(key: str, source: str, args: list[str], info: dict, *, package_source: str | None = None, expected_failure: bool = False, build_only: bool = False) -> tuple[dict, str]:
    row = {"phase": key, "source": source, "status": "INCOMPLETE", "expected_failure": expected_failure}
    info["attempts"].append(row)
    save(info)
    row.update(source_blobs=checkout(source, package_source), package_source=package_source)
    phase = TMP / key
    phase.mkdir()
    events = phase / "raw.jsonl"
    cmd = ["bazelisk", "build" if build_only else "test", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=",
           "--strategy=TestRunner=local", "--build_tag_filters=", "--test_tag_filters=",
           "--nocache_test_results", "--test_timeout=300", "--flaky_test_attempts=1", "--runs_per_test=1",
           "--test_sharding_strategy=disabled", "--nobuild_event_json_file_path_conversion",
           f"--build_event_json_file={events}", "--build_event_publish_all_actions", *args]
    row.update(started_at=utc(), arguments=cmd)
    save(info)
    remaining = DEADLINE - time.monotonic() - 120
    assert remaining > 0, "Qualification budget exhausted before launch"
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER") if k in os.environ}
    env.update(LANG="C.UTF-8", TMPDIR=str(TMP))
    with (phase / "console").open("w") as console:
        proc = subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        try:
            proc.wait(timeout=min(1800, remaining))
        except subprocess.TimeoutExpired:
            row["timed_out"] = True
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    row.update(native_exit=proc.returncode, native_finished_at=utc())
    if proc.returncode:
        diagnostic = re.compile(r"^ERROR:|^FATAL:|^WARNING: Download|undefined:|syntax error:")
        row["diagnostics"] = [re.sub(r"(?:https?|grpcs?)://\S+", "<url>", line)[:500]
                              for line in (phase / "console").read_text(errors="replace").splitlines()
                              if diagnostic.search(line)][:6]
    inline = re.compile(r"^(?:pkg/buffer/)?(?:view|chunk)\.go:[0-9]+:[0-9]+: (?:can|cannot) inline (?:\(\*View\)\.(?:AsSlice|Capacity|Full|AvailableSize)|\(\*chunk\)\.bytes)(?: with cost [0-9]+ as: .*|: .*)$")
    row["inline_remarks"] = [line[:1000] for line in (phase / "console").read_text(errors="replace").splitlines() if inline.fullmatch(line)][:16]
    call = re.compile(r"^(?:pkg/buffer/)?read_qualification_test\.go:([0-9]+):[0-9]+: inlining call to \(\*View\)\.AsSlice$")
    row["inline_calls"] = [line for line in (phase / "console").read_text(errors="replace").splitlines() if call.fullmatch(line)][:16]
    save(info)
    if events.is_file():
        records = [json.loads(line) for line in events.read_text().splitlines()]
        row["invocations"] = [e["started"]["uuid"] for e in records if "started" in e]
        observations = [e for e in records if "testResult" in e.get("id", {})]
        row["attempt_count"] = len(observations)
        save(info)
    else:
        raise ValueError("No original BEP available")
    if build_only or os.environ.get("RACE_DIAGNOSTIC") == "1":
        row["actions"] = []
        for event in records:
            if "action" not in event:
                continue
            action = event["action"]
            if action.get("success") and action.get("type") != "GoStdlib":
                continue
            item = {key: action.get(key) for key in ("type", "success", "exitCode", "label")}
            item["primary_output"] = event.get("id", {}).get("actionCompleted", {}).get("primaryOutput")
            row["actions"].append(item)
            save(info)
            for stream in ("stderr", "stdout"):
                source_log = action.get(stream, {})
                uri = urllib.parse.urlsplit(source_log.get("uri", ""))
                if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                    item[stream] = {"disposition": "UNAVAILABLE_LOCAL_FILE"}
                    continue
                path = Path(urllib.parse.unquote(uri.path)).resolve()
                allowed = (Path.home() / ".cache/bazel").resolve()
                if not path.is_relative_to(allowed) or not path.is_file() or path.stat().st_size > 65536:
                    item[stream] = {"disposition": "OUTSIDE_ROOT_OR_MISSING_OR_OVERSIZED"}
                    continue
                data = path.read_bytes()
                assert not FORBIDDEN.search(data), "Unsafe compiler diagnostic"
                lines = [re.sub(r"(?:https?|grpcs?)://\S+", "<url>", line).replace(str(Path.home()), "<home>")[:1000]
                         for line in data.decode(errors="replace").splitlines()[:40]]
                item[stream] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "lines": lines}
            save(info)
        if build_only:
            assert not observations, "Compiler preflight must not execute tests"
            row.update(status="BUILD_PASSED" if row["native_exit"] == 0 else "BUILD_FAILED", verified_at=utc())
            save(info)
            return row, ""
    assert len(observations) == 1, "Expected one original TestResult"
    event, = observations
    identity, payload = event["id"]["testResult"], event["testResult"]
    row.update(identity=identity, test_status=payload["status"],
               runner=payload.get("executionInfo", {}).get("strategy"), cached_locally=payload.get("cachedLocally", False),
               cached_remotely=payload.get("executionInfo", {}).get("cachedRemotely", False))
    try:
        row["execution_finished_at"] = finish_time(payload)
    except (KeyError, ValueError, TypeError):
        row["execution_finished_at"] = None
    save(info)
    capture = report.capture(events, phase / "events.jsonl", Path("bazel-out"), phase / "reports")
    row["capture_errors"] = len(capture["errors"])
    save(info)
    assert not capture["errors"], "Original report capture incomplete"
    for extension in ("log", "xml"):
        original, = (phase / "reports").rglob("test." + extension)
        data = original.read_bytes()
        assert len(data) < 1024**2 and not FORBIDDEN.search(data), "Unsafe or oversized original test report"
        (OUT / f"{key}.{extension}").write_bytes(data)
        row[extension] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    xml = OUT / f"{key}.xml"
    row["cases"] = cases(xml)
    row["xml"] |= report.xml_cases(xml)
    save(info)
    assert identity["label"] == args[-1], "Wrong observed owner"
    assert all(identity.get(k, 1) == 1 for k in ("run", "shard", "attempt")), "Unexpected repeat or shard"
    assert row["runner"] in ("local", "standalone"), "TestRunner was not coordinator-local"
    assert not row["cached_locally"] and not row["cached_remotely"], "Cached test attempt"
    assert git("rev-parse", "HEAD") == source, "Source changed while executing"
    assert not row.get("timed_out"), "Native command exceeded its budget"
    log = (OUT / f"{key}.log").read_text()
    return row, log


def functional(info: dict) -> None:
    for key, source, args in (
        ("ordinary", PRODUCT, ["--test_arg=-test.v", LABEL]),
        ("race", PRODUCT, ["--config=race", "--test_arg=-test.v", LABEL]),
        ("nogo", PRODUCT, [LABEL + "_nogo"]),
        ("restore-negative", REGRESSION, ["--test_arg=-test.run=^TestExternalStorageRestoreOrder$", "--test_arg=-test.v", LABEL]),
    ):
        try:
            row, log = invoke(key, source, args, info, expected_failure=key == "restore-negative")
            if key == "restore-negative":
                if row["native_exit"] == 0:
                    row["status"] = "DISAGREEMENT"
                assert row["native_exit"] == 3 and row["test_status"] == "FAILED", "Baseline restore failure not observed"
                assert all(row["cases"].get(name) == "FAILED" for name in RESTORE_CASES), "Both original restore leaves must fail"
                assert set(row["cases"]) <= RESTORE_CASES | {"TestExternalStorageRestoreOrder"}, "Unexpected negative cases"
                assert log.count(FAILURE) >= 2, "Missing original release-before-load diagnostics"
                row["status"] = "EXPECTED_FAILURE"
            else:
                assert row["native_exit"] == 0 and row["test_status"] == "PASSED", "Candidate test command failed"
                assert row["xml"]["failed"] == row["xml"]["skipped"] == 0, "Failed or skipped candidate cases"
                if key == "nogo":
                    assert all(f"{arch}...PASS" in log for arch in ("amd64", "arm64")), "Missing Nogo architecture section"
                else:
                    required = RESTORE_CASES | {"TestSaveRestoreExternalBuffers", "TestSaveRestoreEmptyExternalView"}
                    assert all(row["cases"].get(name) == "PASSED" for name in required), "Missing required restore pass"
                row["status"] = "PASS"
            row["verified_at"] = utc()
        except (AssertionError, OSError, ValueError, TypeError, KeyError) as error:
            info["attempts"][-1]["verification_error"] = str(error)[:500]
        save(info)
    ordinary, race = info["attempts"][:2]
    if ordinary.get("status") == race.get("status") == "PASS":
        assert ordinary["identity"]["configuration"] != race["identity"]["configuration"], "Race reused ordinary configuration"
        assert ordinary["cases"] == race["cases"], "Ordinary/race case sets differ"


def measure(block: int, arm: str, info: dict) -> None:
    source = {"parent": BASE, "public": PUBLIC, "candidate": PRODUCT}[arm]
    row, log = invoke(f"block-{block:02d}-{arm}", PRODUCT, ["--test_env=GOMAXPROCS=1", "--test_env=GOGC=100",
        f"--run_under=/usr/bin/taskset -c {info['host']['cpu']}", "--test_arg=-test.run=^TestReadAtMultiViewQualification$", "--test_arg=-test.v",
        "--test_arg=-test.bench=^(BenchmarkReadAt|BenchmarkReadAtMultiView|BenchmarkViewListAsSlice)$",
        "--test_arg=-test.benchtime=300ms", "--test_arg=-test.benchmem", LABEL], info, package_source=source)
    assert row["native_exit"] == 0 and row["test_status"] == "PASSED", "Benchmark attempt failed"
    assert row["cases"] == {"TestReadAtMultiViewQualification": "PASSED"}, "Benchmark layout test did not execute"
    matches = [m for line in log.splitlines() if (m := BENCH_ROW.fullmatch(line))]
    assert Counter(m[1] for m in matches) == Counter(BENCH_NAMES), "Missing or duplicate measured benchmark"
    assert all(int(m[2]) > 1 and float(m[3]) > 0 for m in matches), "Invalid benchmark observation"
    assert "BUFFER_READ_CONTROL views=8 bytes=512 read=400 offset=10" in log, "Multi-view payload/layout control absent"
    with (OUT / arm).open("a") as output:
        print(*(m[0] for m in matches), sep="\n", file=output)
    row.update(status="PASS", benchmark_rows=[m[0] for m in matches], verified_at=utc())
    save(info)


def main() -> None:
    host = {"arch": platform.machine(), "kernel": platform.release(), "cpu": min(os.sched_getaffinity(0))}
    assert host["arch"] == "x86_64" and shutil.which("taskset") == "/usr/bin/taskset", "Unsupported native benchmark host"
    assert re.fullmatch(r"[0-9a-f]{40}", PRODUCT), "Product must be immutable"
    assert git("rev-parse", PUBLIC + "^") == BASE
    assert git("rev-parse", REGRESSION + "^") == PUBLIC
    assert git("diff", "--name-only", PUBLIC, REGRESSION) == TEST
    assert source_bytes(REGRESSION, TEST) == source_bytes(PRODUCT, TEST), "Restore regression differs between arms"
    assert len({source_bytes(source, BUILD) for source in (BASE, PUBLIC, PRODUCT)}) == 1, "Benchmark library settings changed"
    for source in (BASE, PUBLIC, PRODUCT):
        assert git("ls-tree", "--name-only", f"{source}:pkg/buffer").splitlines() == [Path(path).name for path in PACKAGE_FILES], "Package file inventory changed"
    assert len({source_bytes(source, ".bazelversion") for source in (BASE, PUBLIC, PRODUCT)}) == 1
    original = re.compile(rb"func BenchmarkReadAt\(b \*testing.B\) \{.*?\n\}", re.S)
    assert len({original.search(source_bytes(source, TEST))[0] for source in (BASE, PUBLIC, PRODUCT)}) == 1, "Existing benchmark changed"
    info = {"status": "INCOMPLETE", "base": BASE, "public": PUBLIC, "product": PRODUCT, "regression": REGRESSION,
            "host": host, "benchmark_fixture": report.file_record(TMP / "buffer_read_benchmark.go"), "attempts": [],
            "benchmark_common_root": PRODUCT,
            "go_sdk_inputs": {path: git("rev-parse", f"{PRODUCT}:{path}") for path in ("go.mod", "MODULE.bazel", "tools/go_types_memoize.patch")},
            "timing_scope": "Native hot heap-backed Buffer.ReadAt and View.AsSlice on one common dependency root; no packet or end-to-end restore throughput claim"}
    save(info)
    if os.environ.get("RACE_DIAGNOSTIC") == "1":
        original = json.loads((OUT / "original-verdict.json").read_bytes())
        assert original["product"] == PRODUCT and original["status"] == "FAIL"
        race, = (row for row in original["attempts"] if row["phase"] == "race")
        assert race["source"] == PRODUCT and race["native_exit"] == 1 and race["attempt_count"] == 0
        assert race["invocations"] == ["b0da347d-662c-46ac-8029-495e1b341afd"]
        info["original"] = {"run": 41767076495, "artifact": 13293284072,
                            "sha256": "46db1cb9779c30b660cb51fe771e37d71f4f14b13b8eb3fafa42b06e60e3d8ab",
                            "status": original["status"], "race": race}
        try:
            row, _ = invoke("race", PRODUCT, ["--config=race", "--test_arg=-test.v", LABEL], info)
            assert row["native_exit"] == 0 and row["test_status"] == "PASSED", "Actual race test owner failed"
            ordinary, = (item for item in original["attempts"] if item["phase"] == "ordinary")
            assert ordinary["status"] == "PASS" and ordinary["source"] == PRODUCT
            assert row["cases"] == ordinary["cases"], "Race must run the same actual candidate cases"
            assert row["identity"]["configuration"] != ordinary["identity"]["configuration"], "Race configuration was not distinct"
            assert row["xml"]["failed"] == row["xml"]["skipped"] == 0
            row.update(status="PASS", verified_at=utc())
            info["status"] = "RACE_PASS_ORIGINAL_BUILD_FAILURE_UNEXPLAINED"
        except (AssertionError, OSError, ValueError, TypeError, KeyError) as error:
            row = info["attempts"][-1]
            row["verification_error"] = str(error)[:500]
            info["status"] = "FAIL" if row.get("native_exit") not in (None, 0) and not row.get("timed_out") else "INCOMPLETE"
        finally:
            info["verified_at"] = utc()
            save(info)
        assert info["status"] == "RACE_PASS_ORIGINAL_BUILD_FAILURE_UNEXPLAINED", "Race qualification remains unsuccessful"
        return
    try:
        functional(info)
        for block, order in enumerate(list(itertools.permutations(("parent", "public", "candidate"))) * 3, 1):
            for arm in order:
                measure(block, arm, info)
        assert len(info["attempts"]) == 58, "Incomplete comparison"
        assert all(row["status"] == ("EXPECTED_FAILURE" if row["expected_failure"] else "PASS") for row in info["attempts"]), "Some qualification phases did not pass"
        info["status"] = "PASS"
    finally:
        if any(row["status"] == "DISAGREEMENT" or (row.get("native_exit") not in (None, 0) and not row.get("timed_out")
               and not (row["expected_failure"] and row["status"] == "EXPECTED_FAILURE")) for row in info["attempts"]):
            info["status"] = "FAIL"
        info["verified_at"] = utc()
        save(info)


if __name__ == "__main__":
    main()
