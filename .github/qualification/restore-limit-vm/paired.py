"""Verify the two isolated guest capsules and original per-process observations."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from xml.etree import ElementTree

ARMS = ("parent", "candidate")
TMP = Path(os.environ["RUNNER_TEMP"])
PKG = Path(".github/qualification/restore-limit-vm")


def bounded(directory, name, maximum):
    # The namespace is stopped. Never follow an untrusted guest link.
    assert "/" not in name and name not in (".", "..")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        assert stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and 0 < meta.st_size <= maximum
        data = stream.read(maximum + 1)
        assert len(data) == meta.st_size
        return data


def collect(bundle, row, result, save):
    for arm in ARMS:
        digest = hashlib.sha256()
        with (PKG / (arm + ".tar")).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        assert bounded(bundle, arm + "-capsule", 65).decode().strip() == digest.hexdigest()
        elf = (TMP / ("restore-build-" + arm) / "elf.sha256").read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{64}", elf)
        assert bounded(bundle, arm + "-elf", 65).decode().strip() == elf
        row.setdefault("tested_capsules", {})[arm] = {"sha256": digest.hexdigest(), "elf_sha256": elf}
    assert row["tested_capsules"]["parent"]["elf_sha256"] != row["tested_capsules"]["candidate"]["elf_sha256"]
    samples = row["samples"] = []
    lines = {arm: [] for arm in ARMS}
    for i in range(-1, 12):
        for arm in (ARMS[::-1] if i >= 0 and i % 2 else ARMS):
            prefix = f"{i}-{arm}"
            item = {"round": i, "arm": arm}
            samples.append(item)
            item["native_exit"] = int(bounded(bundle, prefix + ".exit", 5))
            log = bounded(bundle, prefix + ".loghash", 65).decode().strip()
            assert re.fullmatch(r"[0-9a-f]{64}", log)
            item["log_sha256"] = log
            save(result)
            assert item["native_exit"] == 0
            xml = bounded(bundle, prefix + ".xml", 256 << 10)
            item["xml_sha256"] = hashlib.sha256(xml).hexdigest()
            case, = ElementTree.fromstring(xml).iter("testcase")
            assert case.get("name") == "TestGraphHookRestore" and case.get("status") != "notrun" and case.get("result") != "suppressed"
            assert all(case.find(tag) is None for tag in ("skipped", "failure", "error"))
            data = json.loads(bounded(bundle, prefix + ".row", 8192))
            assert data["files_verified"] == 100000 and data["memory_limit"] == 2 << 30 and data["direct_child"] is True
            assert data["coordinator_cpus"] == 4 and data["boot"]["--total-memory"] == str(2 << 30)
            assert 0 <= len(data["cpu_quotas"]) <= 20 and all(re.fullmatch(r"max [1-9][0-9]*", q) for q in data["cpu_quotas"])
            assert re.fullmatch(r"[0-9,-]{1,40}", data["sentry_allowed_cpus"])
            assert set(data["boot"]).issubset({"--cpu-num", "--lifecycle-cpu-num", "--total-memory"})
            assert all(re.fullmatch(r"[0-9]{1,12}", value) for value in data["boot"].values())
            assert data["restored_limit"] == 2 << 30 and data["oom_kills"] == 0
            item.update(boot=data["boot"], restored_limit=data["restored_limit"], oom_kills=0)
            item.update(files_verified=100000, memory_limit=data["memory_limit"], native_cpus=4, cpu_quotas=data["cpu_quotas"],
                        sentry_allowed_cpus=data["sentry_allowed_cpus"])
            assert set(data["graph_frames"]) == {"cpuid", "kernel"} and all(type(v) is int and 0 < v < (1 << 30) for v in data["graph_frames"].values())
            item["graph_frames"] = data["graph_frames"]
            assert set(data["archive"]) == {"checkpoint.img", "pages_meta.img", "pages.img"}
            for file in data["archive"].values():
                assert set(file) == {"bytes", "sha256"} and type(file["bytes"]) is int and 0 < file["bytes"] < (4 << 30)
                assert re.fullmatch(r"[0-9a-f]{64}", file["sha256"])
            item["archive"] = data["archive"]
            restore_ns = data["restore_ns"]
            assert type(restore_ns) is int and 0 < restore_ns < 300_000_000_000, restore_ns
            item["restore_ns"] = restore_ns
            if i >= 0:
                lines[arm].append(f"BenchmarkRestore/100000Files 1 {restore_ns} ns/op")
            item["verified"] = True
            save(result)
    assert len({(v["sentry_allowed_cpus"], tuple(v["cpu_quotas"]), tuple(sorted(v["boot"].items()))) for v in samples}) == 1
    for arm in ARMS:
        assert len(lines[arm]) == 12
        (TMP / (arm + ".bench")).write_text("\n".join(lines[arm]) + "\n")
    row["verified_samples"] = len(samples)


def add_fixture(build):
    """Add exactly the reference test source and its only new BUILD dependency."""
    original = build.read_text()
    source = '        "container_save_restore_test.go",\n'
    dep = '        "//pkg/state/statefile",\n'
    assert original.count(source) == original.count(dep) == 1
    updated = original.replace(source, source + '        "gc_reference_test.go",\n')
    build.write_text(updated.replace(dep, dep + '        "//pkg/state/wire",\n'))


def receive(code):
    out = TMP / "restore-vm-verdict.json"
    result = json.loads(out.read_text())
    stage = result["stages"]["restore"] = {"service_exit": code, "status": "INCOMPLETE"}
    def save(value):
        out.write_text(json.dumps(value, indent=2) + "\n")
    save(result)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(TMP / "native-root", flags)
    try:
        # Trusted shell setup and sandbox logs remain private to this worker.
        with (TMP / "native-service.log").open("rb") as service_log:
            diagnostics = service_log.read(65536).decode(errors="replace")
        stage["setup_locations"] = re.findall(r"^HOST_SETUP_LOCATION=((?:service|outer|namespace|pivot):[1-9][0-9]{0,3})$", diagnostics, re.M)[:8]
        stage["payload_locations"] = re.findall(r"^PAYLOAD_LOCATION=([1-9][0-9]{0,3})$", diagnostics, re.M)[:8]
        stage["outer_cpu_max"] = re.findall(r"^CPU_MAX=(max [1-9][0-9]{0,11})$", diagnostics, re.M)
        categories = ("No space left on device", "Permission denied", "Operation not permitted",
                      "No such file or directory", "No medium found", "No such device",
                      "Invalid argument", "wrong fs type", "Device or resource busy",
                      "Read-only file system", "Failed to start transient service")
        categories_lower = {v: v.lower() for v in categories}
        stage["setup_errors"] = [v for v in categories if categories_lower[v] in diagnostics.lower()]
        directory = os.open("outputs", flags, dir_fd=root)
        try:
            collect(directory, stage, result, save)
            assert code == 0 and stage["outer_cpu_max"] == result["host"]["ancestor_cpu_max"] and stage["verified_samples"] == 26
            assert bounded(directory, "summary.xml", 512) == (
                b'<testsuites><testsuite tests="1"><testcase classname="native" name="matchedRestore"/></testsuite></testsuites>\n')
            stage["status"] = result["status"] = "PASS"
        finally:
            os.close(directory)
    except (OSError, ValueError, AssertionError, KeyError, TypeError, ElementTree.ParseError) as exc:
        stage["error"] = type(exc).__name__
        stage["status"] = result["status"] = "FAIL" if code else "INCOMPLETE"
        work = os.open("work", flags, dir_fd=root)
        try:
            try:
                last = bounded(work, "test.log", 8 << 20)
            except (FileNotFoundError, OSError, AssertionError):
                last = b""
            if last:
                stage["last_log_sha256"] = hashlib.sha256(last).hexdigest()
                text = last.decode(errors="replace")
                stage["test_locations"] = list(dict.fromkeys(re.findall(r"\b(?:gc_reference_test|container_test|testutil)\.go:[1-9][0-9]{0,5}\b", text)))[:12]
                stage["test_categories"] = [v for v in (*categories, "cannot set up cgroup", "mandatory cgroup controller",
                    "rootless mode not supported", "unprivileged user", "failed to start", "permission denied",
                    "no such file or directory", "exec format error", "out of memory", "operation not permitted",
                    "read-only file system", "failed to create sandbox", "creating sandbox", "creating gofer",
                    "creating container", "invalid argument", "connection reset by peer",
                    "mount namespace", "failed to mount", "failed to bind", "remount", "chroot",
                    "cgroup.procs", "cgroup.subtree_control", "no space left on device")
                    if v.lower() in text.lower()][:14]
        finally:
            os.close(work)
    finally:
        os.close(root)
        save(result)
    return result["status"] == "PASS"


if __name__ == "__main__":
    if sys.argv[1] == "guard":
        out = TMP / "restore-vm-verdict.json"
        data = json.loads(out.read_text())
        with (TMP / "private-guard.log").open("rb") as stream:
            lines = stream.read(65536).decode(errors="replace").splitlines()
        code = int(sys.argv[2])
        row = data["stages"]["private_guard"] = {
            "exit": code, "product_cases": 0,
            "original_awk_exit": [int(m[1]) for line in lines if (m := re.fullmatch(r"OLD_AWK_EXIT=([0-9]{1,3})", line))],
            "broken_awk_alternative": "OLD_AWK_BROKEN_ALTERNATIVE" in lines,
            "uid_map_verified": "INNER_UID_MAP_VERIFIED" in lines,
            "placement_verified": "PRIVATE_GUARD_PASSED" in lines,
            "locations": [line for line in lines if re.fullmatch(r"(HOST_SETUP_LOCATION=(?:service|outer|namespace|pivot):|PAYLOAD_LOCATION=)[1-9][0-9]{0,3}", line)][:8],
            "prior": {"run": 42021467138, "artifact": 13413007470,
                      "zip_sha256": "e7a221e19f58c8b5fb683ec89f1845d437c149e63b3acb3ad682fa7b753f0b76"},
        }
        good = code == 0 and len(row["original_awk_exit"]) == 1 and row["uid_map_verified"] and row["placement_verified"]
        row["status"] = "PASS" if good else "FAIL"
        out.write_text(json.dumps(data, indent=2) + "\n")
        sys.exit(not good)
    if sys.argv[1] == "receive":
        sys.exit(not receive(int(sys.argv[2])))
    add_fixture(Path(sys.argv[1]))
