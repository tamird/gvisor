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

TMP = Path(os.environ["RUNNER_TEMP"]) / "packet-pages"
STOCK = "1f49f256eae8d2dd2c69ea4cfe9ef2a5bb05516d"
FIXED = "dec1b82660c2974f5b98b2c5ab88d0857b34adb8"
GUEST = "2939db70f24946607cdcc18af08815dc5493742e"
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
                rows.append({"status": result["status"], "cached_locally": result.get("cachedLocally", False),
                             "cached_remotely": info.get("cachedRemotely", False),
                             "strategy": info.get("strategy"), "exit": info.get("exitCode", 0)})
    return rows[0] if len(rows) == 1 else {"result_count": len(rows)}


def ran(row: dict, strategy: str) -> bool:
    return (row.get("status") == "PASSED" and row["exit"] == 0 and row["strategy"] == strategy
            and not row["cached_locally"] and not row["cached_remotely"])


def arm_binaries() -> None:
    assert git("rev-parse", "HEAD^") == FIXED and git("rev-parse", "HEAD^^") == STOCK
    report["fixture_sha256"] = hashlib.sha256((TMP / "packet_ring_linux_test.go").read_bytes()).hexdigest()
    flags = ["--config=aarch64", "-c", "opt", "--jobs=4", "--nostamp", "--remote_cache=", "--remote_executor="]
    for kind, rev in (("stock", STOCK), ("fixed", FIXED)):
        with fixture(rev):
            command("build-arm64-" + kind, ["bazel", "build", *flags, TARGET], 2700)
            outputs = command("path-arm64-" + kind, ["bazel", "cquery", *flags, "--output=files", TARGET], 300).splitlines()
            exe, = [Path(p) for p in outputs if Path(p).is_file() and os.access(p, os.X_OK)]
            assert not exe.is_absolute() and exe.parts[0] == "bazel-out" and ".." not in exe.parts
            dest = TMP / ("arm64-" + kind)
            shutil.copy2(exe, dest)
            raw = dest.read_bytes()
            assert raw[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", raw, 18)[0] == 183
            report["binaries"][kind] = {"source": rev, "sha256": hashlib.sha256(raw).hexdigest(),
                                        "machine": 183, "bytes": len(raw)}
            save()


def controls(strategy: str) -> None:
    assert strategy in ("local", "remote")
    opts = ["bazel"]
    flags = ["--config=x86_64", "-c", "opt", "--jobs=4", "--nostamp", "--nocache_test_results",
             "--flaky_test_attempts=1", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_filter=^" + TEST + "$",
             "--run_under=//test/rbe/netns_runner", "--spawn_strategy=local", "--strategy=TestRunner=" + strategy,
             "--test_output=errors"]
    if strategy == "remote":
        opts.append("--bazelrc=" + str(TMP / "private.bazelrc"))
        flags += ["--config=buildbuddy_remote_executor", "--noremote_local_fallback",
                  "--inject_repository=packet_worker=" + str(TMP / "worker"),
                  "--extra_execution_platforms=@packet_worker//:gnu", "--host_platform=@packet_worker//:gnu",
                  "--remote_download_outputs=toplevel",
                  "--remote_download_regex=^bazel-out/[^/]+/testlogs/pkg/tcpip/link/fdbased/fdbased_test/test[.](xml|log)$"]
    else:
        flags += ["--remote_cache=", "--remote_executor="]
    for kind, rev in (("stock", STOCK), ("fixed", FIXED)):
        with fixture(rev):
            bep = TMP / ("4k-" + kind + ".bep")
            try:
                command("bazel-test-4k-" + kind, [*opts, "test", *flags, "--build_event_json_file=" + str(bep),
                                                  TARGET], 2700, secret=strategy == "remote")
            except RuntimeError:
                # Keep the other kernel comparisons when one worker rejects the namespace.
                pass
            finally:
                log = Path("bazel-testlogs") / PKG / "fdbased_test/test.log"
                text = log.read_text(errors="replace") if log.is_file() else ""
                row = report["4k"][kind] = {"source": rev, "rootless_netns": "ROUTE_PROBE private-test started" in text,
                                            "mapped": "PACKET_RING setup arch=amd64 page=4096 bytes=2101248" in text,
                                            "markers": MARKER in text, "case_passed": "--- PASS: " + TEST in text}
                row["namespace_errno"] = re.findall(r"^ROUTE_PROBE ([a-z-]+) errno=(\d+)$", text, re.M)
                if bep.is_file():
                    row["test_result"] = test_result(bep, TARGET)
                save()
            row["passed"] = (all(row[k] for k in ("rootless_netns", "mapped", "markers", "case_passed"))
                             and ran(row.get("test_result", {}), strategy)
                             and report["commands"]["bazel-test-4k-" + kind]["exit"] == 0)
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
    assert len(report["binaries"]) == len(report["4k"]) == 2
    bep = TMP / "tcg.bep"
    args = ["bazel", "--bazelrc=" + str(TMP / "private.bazelrc"), "test", "--config=rbe",
            "--jobs=80", "--remote_download_outputs=toplevel", "--nocache_test_results",
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
        if bep.is_file():
            report["guest_result"] = test_result(bep, GUEST_TARGET)
        save()
    assert all(report["64k"].values()) and ran(report["guest_result"], "remote")
    assert all(v["passed"] for v in report["4k"].values())
    report["passed"] = True


report = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else {
    "stock": STOCK, "fixed": FIXED, "guest_source": GUEST, "commands": {}, "binaries": {}, "4k": {},
}
try:
    if sys.argv[1] == "build":
        arm_binaries()
    elif sys.argv[1] == "controls":
        controls(sys.argv[2])
    elif sys.argv[1] == "guest":
        prepare_guest()
        guest()
    else:
        raise ValueError(sys.argv[1])
finally:
    save()
