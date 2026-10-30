"""Verify named Linux and gVisor outcomes, retaining individual assertion failures."""

import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


def cases(data):
    found = {}
    for case in ET.fromstring(data).iter("testcase"):
        name = case.get("classname", "") + "." + case.get("name", "")
        if name in found:
            raise ValueError("duplicate case " + name)
        errors = [n.get("message") or n.text or "" for n in case if n.tag in ("failure", "error")]
        skipped = case.find("skipped") is not None or case.get("result") in ("skipped", "suppressed") or case.get("status") != "run"
        found[name] = {"status": "skip" if skipped else "fail" if errors else "pass", "failures": errors}
    return found


def expected_control(rows):
    if set(rows) != {"NetdeviceTest.InterfaceName"}:
        return False
    example = rows["NetdeviceTest.InterfaceName"]
    if example["status"] != "fail" or len(example["failures"]) != 1:
        return False
    msg = example["failures"][0]
    return bool(re.search(r'ifr\..*name\[(?:16|IFNAMSIZ|IF_NAMESIZE)\s*-\s*2\]', msg) and "'x'" in msg and ("'\\0'" in msg or "Which is: 0" in msg))


targets = {
    "socket_netdevice_test": {"NetdeviceTest.InterfaceName"},
    "tuntap_test": {"TuntapTest.CreateInterface", "TuntapTest.SetIffReadOnlyRequest"},
    "ioctl_test": {f"IoctlTest/IoctlTestSIOCGIFCONF.ValidateLoopbackIsPresent/{i}" for i in range(6)},
}
owners = ["//pkg/abi/linux:linux_nogo", "//pkg/sentry/devices/tundev:tundev_nogo", "//pkg/sentry/socket/hostinet:hostinet_nogo"]


def verify(root):
    results = {}
    for lane in ("control", "native", "fixed"):
        path = root / lane
        def read(name):
            f = path / name
            return f.read_text().strip() if f.is_file() else None
        stock = lane == "control"
        arm = {"exit": read("exit"), "source": read("source"), "start": read("start"), "end": read("end"),
               "fixture": read("fixture"), "targets": {}, "errors": []}
        arm["verified"] = arm["source"] == os.environ["STOCK" if stock else "CURRENT"] and arm["exit"] == ("3" if stock else "0") and bool(arm["start"] and arm["end"] and arm["fixture"])
        for name, expected in targets.items():
            if stock and name != "socket_netdevice_test":
                continue
            try:
                rows = cases((path / name / 'test.xml').read_bytes())
                if (path / name / "test.log").stat().st_size == 0:
                    raise ValueError("empty test log")
                arm["targets"][name] = rows
                ok = expected_control(rows) if stock else set(rows) == expected and all(x["status"] == "pass" for x in rows.values())
            except (OSError, ET.ParseError, ValueError) as e:
                arm["errors"].append(f"{name}: {e}")
                ok = False
            arm["verified"] &= ok
        results[lane] = arm
    fixtures = {a["fixture"] for a in results.values()}
    d = root / "owners"
    receipt = (d / "receipt").read_text() if (d / "receipt").is_file() else ""
    statuses = {label: bool(re.search(re.escape(label) + r'\s+(?:\(cached\)\s+)?PASSED\b', receipt)) for label in owners}
    owners_pass = all(statuses.values()) and (d/"exit").is_file() and (d/"exit").read_text().strip() == "0"
    passed = len(fixtures) == 1 and all(a["verified"] for a in results.values()) and owners_pass
    summary = {"verified": passed, "arms": results, "owners": statuses, "same_fixture": len(fixtures) == 1}
    (root / "verdict.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary))
    return passed


if __name__ == "__main__":
    sys.exit(not verify(Path(sys.argv[1])))
