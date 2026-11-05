"""Verify the two isolated guest capsules and original per-process observations."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree

ARMS = ("parent", "candidate")
TMP = Path(os.environ["RUNNER_TEMP"])
PKG = Path(".github/qualification/restore-limit-vm")


def bounded(bundle, name, size):
    full_name = "guest/outputs/" + name
    entry = bundle.getinfo(full_name)
    assert 0 < entry.file_size <= size
    return bundle.read(entry)


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
            assert case.get("name") == "TestGCReferenceSave" and case.get("status") != "notrun" and case.get("result") != "suppressed"
            assert all(case.find(tag) is None for tag in ("skipped", "failure", "error"))
            data = json.loads(bounded(bundle, prefix + ".row", 8192))
            assert data["files_verified"] == 100000 and data["memory_limit"] == 2 << 30 and data["direct_child"] is True
            assert data["coordinator_cpus"] == 4 and data["boot"]["--total-memory"] == str(2 << 30)
            assert 1 <= len(data["cpu_quotas"]) <= 20 and all(re.fullmatch(r"max [1-9][0-9]*", q) for q in data["cpu_quotas"])
            assert re.fullmatch(r"[0-9,-]{1,40}", data["sentry_allowed_cpus"])
            assert set(data["boot"]).issubset({"--cpu-num", "--lifecycle-cpu-num", "--total-memory"})
            assert all(re.fullmatch(r"[0-9]{1,12}", value) for value in data["boot"].values())
            item["boot"] = data["boot"]
            item.update(files_verified=100000, memory_limit=data["memory_limit"], guest_cpus=4, cpu_quotas=data["cpu_quotas"],
                        sentry_allowed_cpus=data["sentry_allowed_cpus"])
            assert set(data["graph_frames"]) == {"cpuid", "kernel"} and all(type(v) is int and 0 < v < (1 << 30) for v in data["graph_frames"].values())
            item["graph_frames"] = data["graph_frames"]
            assert set(data["archive"]) == {"checkpoint.img", "pages_meta.img", "pages.img"}
            for file in data["archive"].values():
                assert set(file) == {"bytes", "sha256"} and type(file["bytes"]) is int and 0 < file["bytes"] < (4 << 30)
                assert re.fullmatch(r"[0-9a-f]{64}", file["sha256"])
            item["archive"] = data["archive"]
            assert data["checkpoint_exit_ns"] >= data["checkpoint_ns"]
            for field in ("checkpoint_ns", "checkpoint_exit_ns", "observed_save_exit_rss", "before_rss", "before_hwm", "lifetime_maxrss", "samples", "max_sample_gap_ns"):
                assert type(data[field]) is int and 0 < data[field] < 300_000_000_000
                item[field] = data[field]
            if i >= 0:
                for field, unit in (("checkpoint_ns", "ns/op"), ("checkpoint_exit_ns", "ns/op"), ("observed_save_exit_rss", "B/op"), ("before_rss", "B/op"), ("before_hwm", "B/op"), ("lifetime_maxrss", "B/op")):
                    lines[arm].append(f"BenchmarkSave/{field} 1 {item[field]} {unit}")
            item["verified"] = True
            save(result)
    assert len({(v["sentry_allowed_cpus"], tuple(v["cpu_quotas"]), tuple(sorted(v["boot"].items()))) for v in samples}) == 1
    for arm in ARMS:
        assert len(lines[arm]) == 72
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


if __name__ == "__main__":
    add_fixture(Path(sys.argv[1]))
