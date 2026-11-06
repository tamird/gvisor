"""Verify the two isolated guest capsules and original per-process observations."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import traceback
from xml.etree import ElementTree

ARMS = ("parent", "reference", "candidate")
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


ERRORS = (OSError, ValueError, AssertionError, KeyError, TypeError, ElementTree.ParseError)
METRICS = (("restore_first_read_ns", "ns/op"), ("restore_reply_ns", "ns/op"),
           ("first_read_rss", "B/op"), ("first_read_hwm", "B/op"))
ORDERS = ("parent reference candidate", "reference candidate parent", "candidate parent reference",
          "parent candidate reference", "candidate reference parent", "reference parent candidate")


def sample(bundle, item):
    i, arm = item["round"], item["arm"]
    prefix = f"{i}-{arm}"
    sequence = int(bounded(bundle, prefix + ".sequence", 8))
    assert sequence == (i+1)*3 + ORDERS[max(i,0)%6].split().index(arm)
    item["sequence"] = sequence
    item["native_exit"] = int(bounded(bundle, prefix + ".exit", 5))
    log = bounded(bundle, prefix + ".loghash", 65).decode().strip()
    assert re.fullmatch(r"[0-9a-f]{64}", log)
    item["log_sha256"] = log
    assert item["native_exit"] == 0
    xml = bounded(bundle, prefix + ".xml", 256 << 10)
    item["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    case, = ElementTree.fromstring(xml).iter("testcase")
    assert case.get("name") == "TestGCReferenceRestore" and case.get("status") != "notrun" and case.get("result") != "suppressed"
    assert all(case.find(tag) is None for tag in ("skipped", "failure", "error"))
    data = json.loads(bounded(bundle, prefix + ".row", 8192))
    # Preserve individually typed observations before checking that the whole
    # workload met its contract. A mismatch in one field cannot erase its peers.
    for key in (*[field for field, _ in METRICS], "files_verified", "memory_limit", "restored_limit",
                "oom_kills", "coordinator_cpus", "sandbox_cpu_usec", "sandbox_throttled_usec"):
        value = data.get(key)
        if type(value) is int and 0 <= value < 300_000_000_000:
            item[key] = value
    if isinstance(data.get("direct_child"), bool):
        item["direct_child"] = data["direct_child"]
    assert data["files_verified"] == 100000 and data["memory_limit"] == 2 << 30 and data["direct_child"] is True
    assert data["coordinator_cpus"] == 4 and data["boot"]["--total-memory"] == str(2 << 30)
    assert 1 <= len(data["cpu_quotas"]) <= 20 and data["cpu_quotas"][0] == "100000 100000" and all(re.fullmatch(r"max [1-9][0-9]*", q) for q in data["cpu_quotas"][1:])
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
    assert data["restore_first_read_ns"] >= data["restore_reply_ns"] > 0
    for field in ("sandbox_cpu_usec", "sandbox_throttled_usec"):
        assert type(data[field]) is int and 0 <= data[field] < 12_000_000_000
        item[field] = data[field]
    for field, _ in METRICS:
        value = data[field]
        assert type(value) is int and 0 < value < 300_000_000_000
        item[field] = value

    # Keep these fields separately from workload validation. A failed executable
    # comparison must still leave the original timing and exit evidence intact.
    evidence = data.get("runtime")
    runtime = {}
    if isinstance(evidence, dict):
        if re.fullmatch("[0-9a-f]{64}", str(evidence.get("selected_sha256", ""))):
            runtime["selected_sha256"] = evidence["selected_sha256"]
        if evidence.get("proc_exe") in ("same-inode", "different-inode", "permission-denied", "unavailable"):
            runtime["proc_exe"] = evidence["proc_exe"]
        if type(evidence.get("boot")) is bool:
            runtime["boot"] = evidence["boot"]
    item["runtime"] = runtime
    item["verified"] = True


def runtime_matches(samples, capsules):
    if set(capsules) != set(ARMS) or len({v["sentry_sha256"] for v in capsules.values()}) != len(ARMS):
        return False
    for item in samples:
        runtime = item.get("runtime", {})
        if runtime.get("selected_sha256") != capsules[item["arm"]]["sentry_sha256"]:
            return False
        # The Sentry can disable ptrace inspection. In that case verify both
        # the sidecar chosen by the boot resolver and the live boot command.
        if runtime.get("proc_exe") != "same-inode" and not (
            runtime.get("proc_exe") == "permission-denied" and runtime.get("boot") is True
        ):
            return False
    return bool(samples)


def collect(bundle, stage, result, save, *, smoke):
    samples = stage["samples"] = []
    rounds = range(-1, 0 if smoke else 36)
    for i in rounds:
        for arm in ARMS:
            item = {"round": i, "arm": arm}
            samples.append(item)
            try:
                sample(bundle, item)
            except ERRORS as exc:
                item["sample_error"] = type(exc).__name__
                item["collector_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
            save(result)

    valid = [item for item in samples if item.get("verified")]
    stage["verified_samples"] = len(valid)
    if not smoke:
        for arm in ARMS:
            lines = [f"BenchmarkRestore/{name} 1 {item[name]} {unit}"
                     for item in valid if item["arm"] == arm and item["round"] >= 0
                     for name, unit in METRICS]
            if lines:
                (TMP / (arm + ".bench")).write_text("\n".join(lines) + "\n")
    save(result)

    capsules = stage["tested_capsules"] = {}
    for arm in ARMS:
        try:
            digest = hashlib.sha256()
            with (PKG / (arm + ".tar")).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            assert bounded(bundle, arm + "-capsule", 65).decode().strip() == digest.hexdigest()
            build = TMP / ("restore-build-" + arm)
            test_elf = (build / "elf.sha256").read_text().strip()
            sentry = (build / "sentry.sha256").read_text().strip()
            assert all(re.fullmatch("[0-9a-f]{64}", h) for h in (test_elf, sentry))
            assert bounded(bundle, arm + "-elf", 65).decode().strip() == test_elf
            capsules[arm] = {"sha256": digest.hexdigest(), "elf_sha256": test_elf, "sentry_sha256": sentry}
        except ERRORS as exc:
            stage.setdefault("capsule_errors", {})[arm] = type(exc).__name__
        save(result)

    stage["runtime_match"] = runtime_matches(valid, capsules)
    if smoke and stage["runtime_match"]:
        shared = {a: dict(v, elf_sha256=capsules["parent"]["elf_sha256"]) for a, v in capsules.items()}
        wrong = {a: dict(v) for a, v in shared.items()}
        wrong["parent"]["sentry_sha256"], wrong["reference"]["sentry_sha256"] = (
            wrong["reference"]["sentry_sha256"], wrong["parent"]["sentry_sha256"])
        stage["accepts_shared_test_elf"] = runtime_matches(valid, shared)
        stage["rejects_swapped_runtime"] = not runtime_matches(valid, wrong)
        save(result)
        assert stage["accepts_shared_test_elf"] and stage["rejects_swapped_runtime"]
    save(result)
    assert len(valid) == 3 * len(rounds) and stage["runtime_match"]
    assert len({(v["sentry_allowed_cpus"], tuple(v["cpu_quotas"]), v["boot"]["--total-memory"]) for v in valid}) == 1


def add_fixture(build):
    """Add exactly the reference test source and its only new BUILD dependency."""
    original = build.read_text()
    source = '        "container_save_restore_test.go",\n'
    dep = '        "//pkg/state/statefile",\n'
    assert original.count(source) == original.count(dep) == 1
    updated = original.replace(source, source + '        "gc_reference_test.go",\n')
    build.write_text(updated.replace(dep, dep + '        "//pkg/state/wire",\n'))


def receive(code, *, smoke=False):
    out = TMP / "restore-vm-verdict.json"
    result = json.loads(out.read_text())
    if smoke:
        out = TMP / "restore-warmup.json"
    stage = result["stages"]["restore"] = {"service_exit": code, "status": "INCOMPLETE"}
    def save(value):
        out.write_text(json.dumps(value, indent=2) + "\n")
    save(result)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(TMP / ("native-smoke" if smoke else "native-root"), flags)
    try:
        # Trusted shell setup and sandbox logs remain private to this worker.
        with (TMP / ("native-smoke.log" if smoke else "native-service.log")).open("rb") as service_log:
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
            collect(directory, stage, result, save, smoke=smoke)
            assert code == 0 and stage["outer_cpu_max"] == result["host"]["ancestor_cpu_max"] and stage["verified_samples"] == (3 if smoke else 111)
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
    if sys.argv[1] in ("receive", "smoke"):
        sys.exit(not receive(int(sys.argv[2]), smoke=sys.argv[1] == "smoke"))
    add_fixture(Path(sys.argv[1]))
