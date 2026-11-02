"""Observe publication of SHM_DEST and the private key during IPC_RMID."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import traceback
import uuid
import xml.etree.ElementTree as ET

from image_execution import execution_records
import qualification_report as reporter

ROOT = Path(os.environ["RUNNER_TEMP"])
PRIVATE = ROOT / "shm-private"
PUBLIC = ROOT / "shm-evidence"
BASE = "aa767e4c43d011121271551fe24da501a56f729d"
FIXED = "166e166261dd9140cfaac183445b2377b3be4f20"
STOCK_PROBE = "0010e1dfdf3778136f099ddc71b846fdde7dd67a"
FIXED_PROBE = "4ebf1e2536f05750fd0f8567b81841c9bf167c37"
NEGATIVE = "3078ecfd48010d70020ace440165cf9e6363a4fc"
PROBE = "//pkg/sentry/kernel/shm:removal_probe_test"
ANALYZER = "//pkg/sentry/kernel/shm:shm_nogo"
SYSCALLS = ["//test/syscalls:shm_test_native", "//test/syscalls:shm_test_runsc_systrap_directfs"]
REPEATED = "NoDestructionOfAttachedSegmentWithMultipleRmid"
CASES = ["RemovedSegmentsAreMarkedDeleted", "RemovedSegmentsAreDestroyed",
         "AllowsAttachToRemovedSegmentWithRefs", "RemovedSegmentsAreNotDiscoverable",
         REPEATED, "RmidOwnershipPermissionDenied"]
PROPERTIES = {"Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
              "workload-isolation-type": "firecracker", "container-image": "docker://" + os.environ["GNU"]}
PRIVATE.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def write(result):
    (PUBLIC / "verdict.json").write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(Path.cwd()), *args])


def phase(result, name, source, labels, extra):
    out = PRIVATE / name
    out.mkdir(exist_ok=True)
    item = {"status": "INCOMPLETE", "verified": False, "source": source, "expected": labels,
            "invocation": str(uuid.uuid4()), "started": now()}
    result["phases"][name] = item
    write(result)
    git("checkout", "--quiet", "--detach", source)
    assert git("rev-parse", "HEAD").decode().strip() == source
    assert not git("diff", "HEAD", "--")
    (out / "commit").write_bytes(git("cat-file", "commit", source))
    (out / "before").write_bytes(git("diff", "HEAD", "--"))
    args = ["test", "--invocation_id=" + item["invocation"], "--config=x86_64", "--jobs=42",
            "--build_tag_filters=", "--test_tag_filters=", "--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_timeout=300",
            "--test_sharding_strategy=disabled", "--test_output=errors", "--strategy=TestRunner=remote",
            "--remote_download_outputs=toplevel", "--remote_download_regex=.*[/]test[.](log|xml)$",
            "--build_event_json_file_path_conversion=false", "--build_event_json_file=" + str(out / "raw.json"),
            "--execution_log_json_file=" + str(out / "spawns.json"), *extra, *labels]
    (out / "arguments").write_text("\n".join(args) + "\n")
    with (out / "bazel.log").open("wb") as log:
        code = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "25m", "bazel",
                               "--noblock_for_lock", "--bazelrc=" + str(ROOT / "gnu.bazelrc"), *args],
                              stdout=log, stderr=log).returncode
    item.update(native_exit=code, process_finished=now(), status="FAIL" if code else "INCOMPLETE")
    write(result)
    (out / "exit").write_text(str(code))
    (out / "after").write_bytes(git("diff", "HEAD", "--"))
    (out / "head").write_bytes(git("rev-parse", "HEAD"))
    captured = reporter.capture(out / "raw.json", out / "bep.json", Path("/"), out / "reports")
    (out / "capture-exit").write_text(str(int(bool(captured["errors"]))))
    context = {"expected_source": source, "commit_file": "commit", "source_before": "before",
               "source_after": "after", "final_head": "head", "executed_arguments": "arguments",
               "native_exit": "exit", "capture_exit": "capture-exit", "command_kind": "test",
               "execution_finished_at": item["process_finished"]}
    (out / "context.json").write_text(json.dumps(context))
    (out / "expected.txt").write_text("\n".join(labels) + "\n")
    verdict = reporter.verdict(out / "bep.json", reporter.manifest_path(out / "bep.json"),
                               out / "context.json", out / "expected.txt")
    item["qualification"] = {key: verdict[key] for key in
                             ("status", "case_counts", "errors", "missing", "bep_finish")}
    if verdict["status"] == "FAIL":
        item["status"] = "FAIL"
    write(result)
    for row in captured["reports"]:
        if row["name"] not in ("test.xml", "test.log"):
            continue
        index = labels.index(row["identity"]["label"])
        destination = PUBLIC / f"{name}-{index}-{row['name']}"
        assert not destination.exists()
        shutil.copyfile(out / row["path"], destination)
        assert reporter.file_record(destination) == {key: row[key] for key in ("sha256", "bytes")}
        item.setdefault("reports", []).append({"identity": row["identity"], "file": destination.name,
                                               "sha256": row["sha256"], "bytes": row["bytes"]})
        write(result)
    assert not verdict["errors"] and not verdict["missing"]
    tests = []
    item["test_actions"] = tests
    with (out / "spawns.json").open() as stream:
        for _, action in execution_records(stream):
            if action is None or action.get("targetLabel") not in labels or action.get("mnemonic") != "TestRunner":
                continue
            args = action["commandArgs"]
            executable = Path(args[0]).name
            assert executable in ("test-setup.sh", "generate-xml.sh")
            props = {entry["name"]: entry["value"] for entry in action["platform"]["properties"]}
            row = {key: action.get(key) for key in ("targetLabel", "runner", "cacheHit", "digest")}
            row.update(executable=executable, exit=action.get("exitCode", 0), status=action.get("status", ""),
                       properties={key: props.get(key) for key in PROPERTIES})
            tests.append(row)
            write(result)
            assert row["runner"] == "remote" and not row["cacheHit"]
            assert row["properties"] == PROPERTIES
            if executable == "test-setup.sh":
                assert Path(args[1]).name == row["targetLabel"].split(":")[1]
    primary = [row for row in tests if row["executable"] == "test-setup.sh"]
    auxiliary = [row for row in tests if row["executable"] == "generate-xml.sh"]
    assert len(primary) == len(labels) and {row["targetLabel"] for row in primary} == set(labels)
    assert len(auxiliary) == len({row["targetLabel"] for row in auxiliary})
    expected_failure = name in ("stock-contention", "negative-guard")
    assert code == (3 if expected_failure else 0)
    assert verdict["status"] == ("FAIL" if expected_failure else "PASS")
    assert all(row["exit"] == (1 if expected_failure else 0) for row in primary)
    assert all(row["exit"] == 0 for row in auxiliary)
    for label in labels:
        entries = [r for r in captured["reports"] if r["identity"]["label"] == label]
        log, = [r for r in entries if r["name"] == "test.log"]
        xml, = [r for r in entries if r["name"] == "test.xml"]
        log = (out / log["path"]).read_text()
        if label == ANALYZER:
            parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", log)
            sections = {arch: body.strip().splitlines() for arch, body in zip(parts[1::2], parts[2::2])}
            item["architectures"] = sections
            write(result)
            assert len(parts) == 5 and set(sections) == {"amd64", "arm64"}
            for arch, lines in sections.items():
                if not expected_failure:
                    assert lines == ["PASS"]
                    continue
                line, = lines
                assert re.fullmatch(r"checklocks: pkg/sentry/kernel/shm/shm\.go:(\d+):\d+: must hold "
                                    r"r\.mu exclusively .+ to call dissociateKeyLocked, but not held "
                                    r"\(locks: .*\) \(GOOARCH=" + arch + r", GOOS=linux\)", line)
                number = int(line.split("shm.go:")[1].split(":")[0])
                assert Path("pkg/sentry/kernel/shm/shm.go").read_text().splitlines()[number - 1].strip() == "r.dissociateKeyLocked(s)"
            continue
        cases = []
        for case in ET.parse(out / xml["path"]).iter("testcase"):
            skipped = case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed"
            failed = case.find("error") is not None or case.find("failure") is not None
            cases.append({"class": case.get("classname"), "name": case.get("name"),
                          "status": "FAIL" if failed else "SKIP" if skipped else "PASS"})
        item.setdefault("cases", {})[label] = cases
        write(result)
        if label == PROBE:
            case, = cases
            assert case["name"] == "TestRemovalPublication" and case["status"] == ("FAIL" if expected_failure else "PASS")
            marker = ("IPC_STAT during removal: key=73 SHM_DEST=" + ("true" if expected_failure else "false")
                      + "; after: key=0 SHM_DEST=true")
            assert marker in log
            assert ("removal published a partial IPC_STAT before acquiring the registry mutex" in log) == expected_failure
            item["observation"] = marker
        else:
            expected = [REPEATED] if name == "stock-repeated" else CASES
            assert len(cases) == len(expected) and {c["name"] for c in cases} == set(expected)
            assert all(c["class"] == "ShmTest" for c in cases)
            assert all(c["status"] == "PASS" or c["status"] == "SKIP" and label.endswith("_native")
                       and c["name"] == "RmidOwnershipPermissionDenied" for c in cases)
    item.update(status=verdict["status"], verified=True)


def main():
    result = {"status": "INCOMPLETE", "phases": {}, "base": BASE, "product": FIXED}
    write(result)
    for commit, parent in [(FIXED, BASE), (NEGATIVE, FIXED), (STOCK_PROBE, BASE), (FIXED_PROBE, FIXED)]:
        assert git("rev-parse", commit + "^").decode().strip() == parent
    for path in ("pkg/sentry/kernel/shm/removal_probe_test.go", "pkg/sentry/kernel/shm/BUILD"):
        assert git("rev-parse", STOCK_PROBE + ":" + path) == git("rev-parse", FIXED_PROBE + ":" + path)
    result["earlier_preflight"] = {"run": 41393938887, "artifact": 13117639769,
                                   "sha256": "21f13db36621d9cf9bc94ee52ca00294d3ac6ace8a48e6fae4b3fccaed123bf7",
                                   "bazel_invocations": 0, "outcome": "fixture patch-ID mismatch"}
    path = "pkg/sentry/kernel/shm/shm.go"
    marker = b"mu sync.Mutex `state:\"nosave\"`"
    substitution = b"mu observedRegistryMutex `state:\"nosave\"`"
    suffixes = []
    for head, parent in ((STOCK_PROBE, BASE), (FIXED_PROBE, FIXED)):
        paths = git("diff", "--name-only", parent, head).decode().splitlines()
        assert set(paths) == {path, "pkg/sentry/kernel/shm/BUILD", "pkg/sentry/kernel/shm/removal_probe_test.go"}
        original = git("show", parent + ":" + path)
        instrumented = git("show", head + ":" + path)
        assert original.index(b"type Registry struct") < original.index(marker) < original.index(b"func NewRegistry")
        prefix = original.replace(marker, substitution, 1)
        assert instrumented.startswith(prefix + b"\n// observedRegistryMutex observes")
        suffixes.append(instrumented[len(prefix):])
    assert suffixes[0] == suffixes[1]
    result["mutex_observer_sha256"] = hashlib.sha256(suffixes[0]).hexdigest()
    write(result)
    for name, source, labels, extra in [
        ("stock-contention", STOCK_PROBE, [PROBE], ["--test_env=GO_TEST_WRAP_TESTV=1"]),
        ("fixed-contention", FIXED_PROBE, [PROBE], ["--test_env=GO_TEST_WRAP_TESTV=1"]),
        ("negative-guard", NEGATIVE, [ANALYZER], []),
        ("guard", FIXED, [ANALYZER], []),
        ("syscalls", FIXED, SYSCALLS, ["--test_env=GTEST_FILTER=" + ":".join("ShmTest." + c for c in CASES)]),
        ("stock-repeated", BASE, SYSCALLS[1:], ["--test_env=GTEST_FILTER=ShmTest." + REPEATED]),
    ]:
        try:
            phase(result, name, source, labels, extra)
        except Exception as err:
            result["phases"][name]["collection_error"] = {"type": type(err).__name__,
                                                           "line": traceback.extract_tb(err.__traceback__)[-1].lineno}
        result["phases"][name]["verified_at"] = now()
        write(result)
    phases = result["phases"]
    result.update(status="PASS" if all(v["verified"] for v in phases.values()) else "INCOMPLETE", verified_at=now())
    if any(v["status"] == "FAIL" for k, v in phases.items() if k not in ("stock-contention", "negative-guard")):
        result["status"] = "FAIL"
    write(result)
    raise SystemExit(result["status"] != "PASS")


if __name__ == "__main__":
    main()
