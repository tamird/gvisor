"""Reconcile original SHM evidence and run only the native credential case."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import traceback
import xml.etree.ElementTree as ET
import zipfile


ORIGINAL = "0ba284be13c466f7463e650f80b100198345917b21349a1cba6e0041a1ace04c"
FIXED = "166e166261dd9140cfaac183445b2377b3be4f20"
ALIAS = "ccde34094ac43ea07db674217fda37f918ffa1b9"
OWNER = "//test/rbe/shm_ownership:ownership_test"
CASE = "RmidOwnershipPermissionDenied"
EXPECTED = {CASE, "NoDestructionOfAttachedSegmentWithMultipleRmid", "RemovedSegmentsAreMarkedDeleted",
            "RemovedSegmentsAreDestroyed", "AllowsAttachToRemovedSegmentWithRefs", "RemovedSegmentsAreNotDiscoverable"}


def reconcile(path):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ORIGINAL
    with zipfile.ZipFile(path) as archive:
        original = json.loads(archive.read("verdict.json"))
        assert original["product"] == FIXED and original["status"] == "FAIL"
        phases = original["phases"]
        assert set(phases) == {"negative-guard", "guard", "stock-contention", "fixed-contention", "stock-repeated", "syscalls"}
        names = {"verdict.json"}
        for phase in phases.values():
            for report in phase["reports"]:
                data = archive.read(report["file"])
                assert len(data) == report["bytes"] and hashlib.sha256(data).hexdigest() == report["sha256"]
                assert report["file"] not in names
                names.add(report["file"])
        assert set(archive.namelist()) == names
        assert all(phases[name]["verified"] for name in ("guard", "stock-contention", "fixed-contention", "stock-repeated"))
        assert phases["syscalls"]["native_exit"] == 3 and phases["negative-guard"]["native_exit"] == 3
        diagnostic = archive.read("negative-guard-0-test.log").decode()
        parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", diagnostic)
        per_arch = dict(zip(parts[1::2], (part.strip().splitlines() for part in parts[2::2])))
        assert len(parts) == 5 and set(per_arch) == {"amd64", "arm64"}
        for arch, lines in per_arch.items():
            assert len(lines) == 2 and lines[0] == lines[1]
            assert re.fullmatch(r"checklocks: pkg/sentry/kernel/shm/shm\.go:725:24: must hold "
                                r"r\.mu exclusively .+ to call dissociateKeyLocked, but not held "
                                r"\(locks: .+\) \(GOOARCH=" + arch + r", GOOS=linux\)", lines[0])
        cases = {}
        failures = []
        for index, label in enumerate(phases["syscalls"]["expected"]):
            source = f"syscalls-{index}-test.xml"
            report, = [r for r in phases["syscalls"]["reports"] if r["file"] == source]
            assert report["identity"]["label"] == label
            outcomes = {}
            for row in ET.fromstring(archive.read(source)).iter("testcase"):
                name = row.attrib["name"]
                assert row.get("classname") == "ShmTest" and name not in outcomes
                assert row.get("status") != "notrun" and row.get("result") != "suppressed"
                assert row.find("skipped") is None and row.find("error") is None
                errors = [node.attrib["message"] for node in row.findall("failure")]
                outcomes[name] = "FAIL" if errors else "PASS"
                if errors:
                    assert label.endswith("_native") and name == CASE
                    failures.extend(errors)
            assert set(outcomes) == EXPECTED
            assert all(v == ("FAIL" if label.endswith("_native") and n == CASE else "PASS")
                       for n, v in outcomes.items())
            cases[label] = outcomes
        assert len(failures) == 5
        assert "scratch_gid" in failures[0] and "Invalid argument" in failures[0]
        assert "scratch_uid" in failures[1] and "Invalid argument" in failures[1]
    return {"run": 41394136215, "artifact": 13119345175, "sha256": ORIGINAL, "original_status": "FAIL",
            "original_phases": {n: {"source": p["source"], "invocation": p["invocation"], "native_exit": p["native_exit"],
                                     "finished_at": p["process_finished"]} for n, p in phases.items()},
            "observations": {n: phases[n]["observation"] for n in ("stock-contention", "fixed-contention")},
            "guard": phases["guard"]["architectures"], "missing_guard": per_arch, "syscalls": cases,
            "native_ownership_failures": failures, "stock_repeated": phases["stock-repeated"]["cases"]}


def main():
    import shm_removal as scope

    result = {"status": "INCOMPLETE", "phases": {}, "product": FIXED,
              "retained_original": reconcile(scope.ROOT / "original.zip")}
    scope.write(result)
    assert scope.git("rev-parse", ALIAS + "^").decode().strip() == FIXED
    assert scope.git("diff", "--name-only", FIXED, ALIAS).decode().splitlines() == ["test/rbe/shm_ownership/BUILD"]
    scope.CASES = [CASE]
    try:
        scope.phase(result, "direct-native", ALIAS, [OWNER], ["--test_env=GTEST_FILTER=ShmTest." + CASE])
    except Exception as exc:
        result["phases"]["direct-native"]["collection_error"] = {"type": type(exc).__name__,
                                                                  "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    row = result["phases"]["direct-native"]
    result.update(status="PASS" if row["verified"] else row["status"], verified_at=scope.now())
    scope.write(result)
    raise SystemExit(result["status"] != "PASS")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        print(json.dumps(reconcile(Path(sys.argv[1])), indent=2))
    else:
        main()
