"""Build identical UDP probes from each revision and run them on two Linux kernels."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import uuid

TMP = Path(os.environ["RUNNER_TEMP"]) / "packet-pages"
STOCK = "1f49f256eae8d2dd2c69ea4cfe9ef2a5bb05516d"
FIXED = "dec1b82660c2974f5b98b2c5ab88d0857b34adb8"
GUEST = "2939db70f24946607cdcc18af08815dc5493742e"
SCRATCH = "9fbe0ff5121eb3b1c34c72a9b41b5f35ec3eb788"
PKG = Path("pkg/tcpip/link/fdbased")
TARGET = "//pkg/tcpip/link/fdbased:fdbased_test"
GUEST_PKG = Path("test/rbe/packet_pages")
GUEST_TARGET = "//test/rbe/packet_pages:on_64k"
TEST = "TestPacketMMapDeliversUDP"
MARKER = "PACKET_RING delivered 40 sequential UDP markers"
RECEIPT = TMP / "receipt.json"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def save() -> None:
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n")


def command(key: str, args: list[str], limit: int, *, secret: bool = False) -> str:
    report["phase"] = key
    save()
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=limit)
    row = report["commands"][key] = {"exit": proc.returncode,
                                      "stdout_sha256": hashlib.sha256(proc.stdout.encode()).hexdigest(),
                                      "stderr_sha256": hashlib.sha256(proc.stderr.encode()).hexdigest()}
    if proc.returncode:
        row["undefined"] = sorted(set(re.findall(r"undefined symbol: ([\w]+)", proc.stderr)))
        row["packages"] = sorted(set(re.findall(r"no such package '([@\w+/.:~-]+)'", proc.stderr)))
        row["categories"] = [s for s in ("No space left on device", "no matching toolchains", "is incompatible",
                                         "timed out", "forbidden", "does not have a dependency declared",
                                         "undefined:", "syntax error", "not found", "is not declared",
                                         "Failed to download", "different digest") if s.lower() in proc.stderr.lower()]
        if not secret:
            row["errors"] = [line[:400] for line in proc.stderr.splitlines() if line.startswith("ERROR:")
                             or re.search(r"[.]go:[0-9]+:", line)][:4]
    save()
    if proc.returncode:
        raise RuntimeError(key)
    return proc.stdout


@contextmanager
def fixture(rev: str):
    git("checkout", "--detach", rev)
    assert git("rev-parse", "HEAD") == rev
    build = PKG / "BUILD"
    expected = build.read_text()
    anchor = '        "endpoint_test.go",\n'
    assert expected.count(anchor) == 1
    target_fixture = PKG / "packet_ring_linux_test.go"
    wrapper = Path("test/rbe/netns_runner")
    assert not target_fixture.exists() and not wrapper.exists()
    build.write_text(expected.replace(anchor, anchor + '        "packet_ring_linux_test.go",\n'))
    shutil.copyfile(TMP / target_fixture.name, target_fixture)
    shutil.copytree(TMP / "netns_runner", wrapper)
    try:
        yield
    finally:
        target_fixture.unlink()
        shutil.rmtree(wrapper)
        git("restore", "--", str(build))


def test_result(bep: Path, target: str) -> dict:
    rows = []
    with bep.open() as events:
        for line in events:
            event = json.loads(line)
            key = event.get("id", {}).get("testResult", {})
            if key.get("label") == target:
                result = event["testResult"]
                info = result.get("executionInfo", {})
                row = {"status": result["status"], "cached_locally": result.get("cachedLocally", False),
                       "cached_remotely": info.get("cachedRemotely", False),
                       "strategy": info.get("strategy"), "exit": info.get("exitCode", 0)}
                for output in result.get("testActionOutput", []):
                    if output.get("name") == "test.log":
                        digest = re.search(r"/blobs/([a-f0-9]{64})/(\d+)$", output.get("uri", ""))
                        if digest:
                            row["test_log"] = {"sha256": digest[1], "bytes": int(digest[2])}
                rows.append(row)
    return rows[0] if len(rows) == 1 else {"result_count": len(rows)}


def ran(row: dict, strategy: str) -> bool:
    return (row.get("status") == "PASSED" and row["exit"] == 0 and row["strategy"] == strategy
            and not row["cached_locally"] and not row["cached_remotely"])


def arm_binaries() -> None:
    assert git("rev-parse", FIXED + "^") == STOCK
    report["fixture_sha256"] = hashlib.sha256((TMP / "packet_ring_linux_test.go").read_bytes()).hexdigest()
    flags = ["--config=aarch64", "-c", "opt", "--jobs=4", "--nostamp", "--remote_cache=", "--remote_executor="]
    for kind, rev in (("stock", STOCK), ("fixed", FIXED)):
        if kind in report["binaries"]:
            continue
        with fixture(rev):
            command("build-arm64-" + kind, ["bazel", "build", *flags, TARGET], 2700)
            outputs = command("path-arm64-" + kind, ["bazel", "cquery", *flags, "--output=files", TARGET], 300).splitlines()
            exe, = [Path(p) for p in outputs if Path(p).is_file() and os.access(p, os.X_OK)]
            assert not exe.is_absolute() and exe.parts[0] == "bazel-out" and ".." not in exe.parts
            dest = TMP / ("arm64-" + kind)
            shutil.copy2(exe, dest)
            raw = dest.read_bytes()
            original = json.loads((TMP / "original-receipt.json").read_text())["binaries"][kind]
            assert hashlib.sha256(raw).hexdigest() == original["sha256"] and len(raw) == original["bytes"]
            assert raw[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", raw, 18)[0] == 183
            report["binaries"][kind] = {"source": rev, "sha256": hashlib.sha256(raw).hexdigest(),
                                        "machine": 183, "bytes": len(raw)}
            save()


def controls() -> None:
    git("checkout", "--detach", SCRATCH)
    assert git("rev-parse", "HEAD") == SCRATCH
    paths = [str(PKG / name) for name in ("BUILD", "endpoint.go", "endpoint_test.go", "mmap.go", "mmap_unsafe.go",
                                          "packet_dispatchers.go", "processors.go")]
    test_path = str(PKG / "packet_ring_linux_test.go")
    flags = ["--config=x86_64", "--extra_execution_platforms=//test/rbe/release_prewarmer:amd64",
             "--host_platform=//test/rbe/release_prewarmer:amd64", "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--extra_toolchains=//test/rbe/protoc_check:static_python", "--noincompatible_exclude_starlark_flags_from_exec_config",
             "--lockfile_mode=off", "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results",
             "--test_env=GO_TEST_WRAP_TESTV=1", "--runs_per_test=1", "--flaky_test_attempts=1",
             "--run_under=//test/rbe/netns_runner", "--test_filter=^" + TEST + "$", "--jobs=32",
             "--spawn_strategy=remote", "--noremote_local_fallback", "--strategy=TestRunner=remote",
             "--modify_execution_info=^TestRunner$=+no-remote-cache", "--remote_download_outputs=toplevel",
             "--remote_download_regex=^bazel-out/[^/]+/testlogs/pkg/tcpip/link/fdbased/fdbased_test/test[.](xml|log)$"]
    for kind, rev in (("stock", STOCK), ("fixed", FIXED)):
        git("restore", "--source=" + rev, "--worktree", "--", *paths)
        git("diff", "--exit-code", rev, "--", str(PKG), ":!" + test_path)
        build = PKG / "BUILD"
        expected = build.read_text()
        anchor = '        "endpoint_test.go",\n'
        assert expected.count(anchor) == 1
        build.write_text(expected.replace(anchor, anchor + '        "packet_ring_linux_test.go",\n'))
        shutil.copyfile(TMP / Path(test_path).name, test_path)
        bep = TMP / ("4k-" + kind + ".bep")
        invocation = str(uuid.uuid4())
        try:
            command("bazel-test-4k-" + kind,
                    [str(TMP / "scratch-bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(TMP / "private.bazelrc"),
                     "test", "--config=buildbuddy_remote_executor", *flags, "--invocation_id=" + invocation,
                     "--build_event_json_file=" + str(bep), TARGET], 1100, secret=True)
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        finally:
            log = Path("bazel-testlogs") / PKG / "fdbased_test/test.log"
            text = log.read_text(errors="replace") if log.is_file() else ""
            row = report["4k"][kind] = {"source": rev, "scratch_harness": SCRATCH, "invocation": invocation,
                                        "rootless_netns": "ROUTE_PROBE private-test started" in text,
                                        "mapped": "PACKET_RING setup arch=amd64 page=4096 bytes=2101248" in text,
                                        "markers": MARKER in text, "case_passed": "--- PASS: " + TEST in text}
            row["namespace_errno"] = re.findall(r"^ROUTE_PROBE ([a-z-]+) errno=(\d+)$", text, re.M)
            if bep.is_file():
                row["test_result"] = test_result(bep, TARGET)
            row["passed"] = (all(row[k] for k in ("rootless_netns", "mapped", "markers", "case_passed"))
                             and ran(row.get("test_result", {}), "remote")
                             and report["commands"].get("bazel-test-4k-" + kind, {}).get("exit") == 0)
            git("restore", "--worktree", "--", *paths, test_path)
            save()


def prepare_guest() -> None:
    git("checkout", "--detach", GUEST)
    assert git("rev-parse", "HEAD") == GUEST and not GUEST_PKG.exists()
    GUEST_PKG.mkdir()
    for src, name in (("guest.BUILD", "BUILD"), ("guest.sh", "guest.sh"),
                      ("arm64-stock", "stock"), ("arm64-fixed", "fixed")):
        shutil.copy2(TMP / src, GUEST_PKG / name)


def guest() -> None:
    assert git("rev-parse", "HEAD") == GUEST
    assert len(report["binaries"]) == 2
    bep = TMP / "tcg.bep"
    invocation = str(uuid.uuid4())
    report["guest_invocation"] = invocation
    args = ["bazel", "--bazelrc=" + str(TMP / "private.bazelrc"), "test", "--config=rbe",
            "--jobs=80", "--invocation_id=" + invocation, "--remote_download_outputs=toplevel", "--nocache_test_results",
            "--flaky_test_attempts=1", "--test_output=errors", "--build_event_json_file=" + str(bep), GUEST_TARGET]
    try:
        command("64k-tcg", args, 4200, secret=True)
    finally:
        log = Path("bazel-testlogs") / GUEST_PKG / "on_64k" / "test.log"
        text = log.read_text(errors="replace") if log.is_file() else ""
        report["64k"] = {"kernel": "6.8.0-138-generic-64k" in text, "page_size": "Guest page size: 65536" in text,
                         "stock_rejected": "tpBlockSize: 2101248 is not page aligned, pagesize: 65536" in text,
                         "fixed_mapped": "PACKET_RING setup arch=arm64 page=65536 bytes=2162688" in text,
                         "fixed_markers": MARKER in text, "fixed_passed": "--- PASS: " + TEST in text}
        report["diagnostics"] = [line[:200] for line in text.splitlines() if
                                 re.search(r"packet_ring_linux_test[.]go:[0-9]+|stock unexpectedly|"
                                           r"PACKET_RING missing executable|command not found|No such file or directory|Permission denied|"
                                           r"Exec format error|phase=guest_status_read", line, re.I)
                                 and not re.search(r"(?i)token|secret|api.key|authorization|password|https?://",line)][:10]
        if bep.is_file():
            report["guest_result"] = test_result(bep, GUEST_TARGET)
        save()
    assert all(report["64k"].values()) and ran(report["guest_result"], "remote")
    report["guest_passed"] = True


report = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else {
    "stock": STOCK, "fixed": FIXED, "guest_source": GUEST, "commands": {}, "binaries": {}, "4k": {},
}
try:
    if sys.argv[1] == "build":
        arm_binaries()
    elif sys.argv[1] == "controls":
        controls()
    elif sys.argv[1] == "finish":
        assert report["guest_passed"] and len(report["4k"]) == 2 and all(v["passed"] for v in report["4k"].values())
        report["passed"] = True
    elif sys.argv[1] == "guest":
        prepare_guest()
        guest()
    else:
        raise ValueError(sys.argv[1])
finally:
    save()
