"""Verify named syscall assertions and the analyzer, including stock failures."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
required = {"TuntapTest.CreateInterface", "TuntapTest.SetIffReadOnlyRequest"}
result = {}
for name in ("current-native", "stock-guest", "current-guest"):
    directory = root / name
    def read(filename):
        file = directory / filename
        return file.read_text().strip() if file.exists() else None
    xml = directory / "test.xml"
    cases = {}
    if xml.is_file() and xml.stat().st_size:
        for case in ET.parse(xml).iter("testcase"):
            key = case.get("classname", "") + "." + case.get("name", "")
            failures = [node.text or node.get("message", "") for node in case if node.tag in ("failure", "error")]
            skipped = case.find("skipped") is not None or case.get("result") in ("skipped", "suppressed") or case.get("status") == "notrun"
            cases[key] = {"status": "SKIP" if skipped else "FAIL" if failures else "PASS", "failures": failures}
    source = read("source")
    expected_source = "43c2372344307be8699cf4e9833de413f9f892da" if name == "stock-guest" else "0625467a2ad37361a730bbc06fe0cbf6462abc34"
    expected_target = "//test/syscalls:tuntap_test_" + ("native" if name == "current-native" else "runsc_systrap_directfs")
    passed = set(cases) == required and source == expected_source and read("target") == expected_target
    if name == "stock-guest":
        name_case = cases.get("TuntapTest.CreateInterface", {})
        fault_case = cases.get("TuntapTest.SetIffReadOnlyRequest", {})
        name_fail = name_case.get("failures", [])
        fault_fail = fault_case.get("failures", [])
        passed &= (read("exit") == "3" and len(name_fail) == 1 and
                   "ifr.ifr_name" in name_fail[0] and "ifr_get.ifr_name" in name_fail[0] and
                   len(fault_fail) == 1 and "TUNSETIFF" in fault_fail[0] and
                   ("EFAULT" in fault_fail[0] or "Bad address" in fault_fail[0] or "errno of 14" in fault_fail[0]) and "Actual: 0" in fault_fail[0])
    else:
        passed &= read("exit") == "0" and all(value["status"] == "PASS" for value in cases.values())
    result[name] = {"source": source, "target": read("target"), "expected_target": expected_target,
                    "expected_cases": sorted(required), "cases": cases, "missing": sorted(required - cases.keys()),
                    "fixture": read("fixture-sha256"), "finished": read("end"),
                    "exit": read("exit"), "verified": passed}
owners = root / "owners"
owner_exit = owners / "exit"
owner_receipt = owners / "receipt"
passed = owner_exit.is_file() and owner_exit.read_text().strip() == "0" and owner_receipt.is_file() and any(
    "//pkg/sentry/devices/tundev:tundev_nogo" in line and "PASSED" in line for line in owner_receipt.read_text().splitlines())
matching = len({r["fixture"] for r in result.values()}) == 1 and all(r["fixture"] for r in result.values())
verified = all(r["verified"] for r in result.values()) and bool(matching) and passed
verdict = {"verified": verified, "lanes": result, "analyzer": bool(passed),
           "verified_at": datetime.now(timezone.utc).isoformat()}
(root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
print(json.dumps(verdict))
if not verified:
    sys.exit(1)
