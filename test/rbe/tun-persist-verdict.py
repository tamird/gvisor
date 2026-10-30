"""Verify named syscall assertions and the analyzer, including stock failures."""

from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
required = {"TuntapTest.CreatePersistentInterface", "TuntapTest.SetPersistAfterLinkDeletion"}
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
    expected_source = "a98e7cd657808a2708dfad7bef8f0b312382ee2a" if name == "stock-guest" else "736b926672075e20b03410b52b6f3308751a7bb4"
    expected_target = "//test/syscalls:tuntap_test_" + ("native" if name == "current-native" else "runsc_systrap_directfs")
    passed = set(cases) == required and source == expected_source and read("target") == expected_target
    if name == "stock-guest":
        bad = cases.get("TuntapTest.SetPersistAfterLinkDeletion", {})
        fs = bad.get("failures", [])
        passed &= (read("exit") == "3" and len(fs) == 2 and
                   all(any(re.fullmatch(
                       rf"test/syscalls/linux/tuntap\.cc:{line}\n"
                       rf"Value of: ioctl\(tun\.get\(\), [^\n]+, {arg}\)\n"
                       r"Expected: -1 \(failure\), with errno PosixError\(errno=77 [^\n]+\)\n"
                       r"  Actual: 0 \(of type int\)\n?", f)
                           for f in fs) for arg, line in [(1, 470), (0, 472)]) and
                   cases.get("TuntapTest.CreatePersistentInterface", {}).get("status") == "PASS")
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
    "//pkg/tcpip/link/tun:tun_nogo" in line and "PASSED" in line for line in owner_receipt.read_text().splitlines())
matching = len({r["fixture"] for r in result.values()}) == 1 and all(r["fixture"] for r in result.values())

orders = {}
for lane, expected_sha, rc, outcome in (
    ("order-before", 'f14171d4b0243f51579ce3f340a8c42743a43fc6', "3", "FAIL"),
    ("order-after", '25c2f461327b014fa23fc2a0f79ba88094a05e95', "0", "PASS"),
):
    path = root / lane
    def get(name):
        p = path / name
        return p.read_text().strip() if p.is_file() else None
    log = get("test.log") or ""
    xml = get("test.xml")
    ran = "=== RUN   TestSetPersistentWhileDeleting" in log and f"--- {outcome}: TestSetPersistentWhileDeleting" in log
    valid = bool(xml and list(ET.fromstring(xml).iter("testcase")))
    observed = "Incrementing non-positive count" in log
    ok = (get("source") == expected_sha and get("target") == "//pkg/tcpip/link/tun:persistence_interleave_test"
          and get("exit") == rc and ran and valid and observed == (outcome == "FAIL"))
    orders[lane] = dict(verified=ok, source=get("source"), expected_exit=rc, exit=get("exit"), observed_panic=observed,
                        target=get("target"), executed=ran, fixture=get("fixture-sha256"), finished=get("end"))
matching_orders = bool(orders["order-before"]["fixture"]) and orders["order-before"]["fixture"] == orders["order-after"]["fixture"]
verified = all(x["verified"] for x in orders.values()) and matching_orders and all(r["verified"] for r in result.values()) and bool(matching) and passed
verdict = {"verified": verified, "lanes": result, "controlled_interleavings": orders, "analyzer_artifact": 13005496962, "analyzer": bool(passed),
           "verified_at": datetime.now(timezone.utc).isoformat()}
(root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
print(json.dumps(verdict))
if not verified:
    sys.exit(1)
