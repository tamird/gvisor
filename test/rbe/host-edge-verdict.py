"""Verify exact Linux and gVisor outcomes for two listener types."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
required = {f"StreamSeqpacket/GoferStreamSeqpacketTest.EpollBindListenWaitAccept/{i}" for i in range(2)}
out = {}
for lane in ("current-native", "stock-guest", "current-guest"):
    directory = root / lane

    def read(key):
        filename = directory / key
        return filename.read_text().strip() if filename.exists() else None

    cases = {}
    xml = directory / "test.xml"
    errors = []
    if xml.exists():
        try:
            for case in ET.parse(xml).iter("testcase"):
                name = case.get("classname", "") + "." + case.get("name", "")
                fail = [n.text or n.get("message", "") for n in case if n.tag in ("failure", "error")]
                skip = case.find("skipped") is not None or case.get("result") in ("skipped", "suppressed") or case.get("status") == "notrun"
                if name in cases:
                    errors.append("duplicate: " + name)
                cases[name] = {"status": "SKIP" if skip else "FAIL" if fail else "PASS", "failures": fail}
        except ET.ParseError as e:
            errors.append(str(e))

    stock = lane == "stock-guest"
    expected = "a3491df53bd5865c3b23deb4ce77e4bebfc47f3e" if stock else "e1fef1717c9875d137ad593d91dd148aee39df83"
    target = "//test/syscalls:bind_external_test_" + ("native" if lane == "current-native" else "runsc_systrap_directfs")
    valid = set(cases) == required and not errors and read("source") == expected and read("target") == target
    valid &= read("exit") == ("3" if stock else "0")
    if stock:
        valid &= all(v["status"] == "FAIL" and len(v["failures"]) == 1 and "250" in v["failures"][0] and "Actual: 1" in v["failures"][0] for v in cases.values())
    else:
        valid &= all(v["status"] == "PASS" for v in cases.values())
    out[lane] = {"source": read("source"), "target": read("target"), "cases": cases, "missing": sorted(required-cases.keys()), "errors": errors,
                 "exit": read("exit"), "fixture": read("fixture-sha256"), "notifier": read("notifier-sha256"), "finished": read("end"), "verified": bool(valid)}

owners = root / "owners"
exitcode = owners / "exit"
receipt = owners / "receipt"
owner_pass = exitcode.exists() and exitcode.read_text().strip() == "0" and receipt.exists() and any(
    "//pkg/fdnotifier:fdnotifier_nogo" in s and "PASSED" in s for s in receipt.read_text().splitlines())
same_fixture = len({v["fixture"] for v in out.values()}) == 1 and all(v["fixture"] for v in out.values())
verified = all(v["verified"] for v in out.values()) and same_fixture and owner_pass
verdict = {"verified": bool(verified), "lanes": out, "analyzer": owner_pass, "verified_at": datetime.now(timezone.utc).isoformat()}
(root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
print(json.dumps(verdict))
if not verified:
    sys.exit(1)
