"""Check the named syscall results, including the expected stock failure."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

from qualification_report import xml_cases


def read(path):
    return path.read_text().strip() if path.is_file() else ""


def lane(root, name):
    path = root / name
    expected = set(os.environ["GTESTS"].split(":"))
    stock = name == "stock-guest"
    source = read(path / "source")
    target = read(path / "target")
    platform = "native" if name == "current-native" else "runsc_systrap_directfs"
    xml = path / "test.xml"
    cases = {}
    counts = {}
    if xml.is_file() and xml.stat().st_size:
        counts = xml_cases(xml)
        for case in ET.parse(xml).iter("testcase"):
            key = case.get("classname") + "." + case.get("name")
            if key in cases:
                raise ValueError(f"Duplicate {key} in {xml}")
            cases[key] = [(failure.text or "").strip() or failure.get("message", "") for failure in case
                          if failure.tag in ("failure", "error")]

    code = read(path / "exit")
    fixture = read(path / "fixture-sha256").partition(" ")[0]
    verified = (source == os.environ["STOCK" if stock else "CURRENT"]
                and target == f"//test/syscalls:semaphore_test_{platform}"
                and fixture == os.environ["FIXTURE"]
                and set(cases) == expected and counts.get("skipped") == 0)
    if stock:
        failures = cases.get("SemaphoreTest.SemCtlGetPid", [])
        verified &= (code == "3" and counts.get("failed") == 1 and len(failures) == 1
                     and re.search(r"semctl\(sem\.get\(\), 0, (?:GETPID|11)\)", failures[0]) is not None
                     and "Expected: is equal to 0" in failures[0]
                     and re.search(r"\bActual:\s*[1-9]\d*\b", failures[0]) is not None)
    else:
        verified &= code == "0" and counts.get("passed") == len(expected)
    diagnostics = read(path / "compiler-diagnostics")
    return dict(source=source, target=target, fixture=fixture, expected=sorted(expected),
                executed=sorted(cases), missing=sorted(expected - cases.keys()), counts=counts,
                failures={key: messages for key, messages in cases.items() if messages},
                exit=code, finished=read(path / "end"), verified=verified,
                invocations=sorted(set(re.findall(r"Invocation ID: ([a-f0-9-]{36})", diagnostics))))


def main():
    root = Path(sys.argv[1])
    lanes = {name: lane(root, name) for name in ("current-native", "stock-guest", "current-guest")}
    receipt = read(root / "owners" / "receipt")
    owner = "//pkg/sentry/kernel/semaphore:semaphore_nogo"
    analyzer = (read(root / "owners" / "exit") == "0"
                and any(owner in line and " PASSED " in line for line in receipt.splitlines()))
    verified = all(result["verified"] for result in lanes.values()) and analyzer
    verdict = dict(lanes=lanes, analyzer={owner: analyzer, "exit": read(root / "owners" / "exit"),
                                      "finished": read(root / "owners" / "end")}, verified=verified,
                   verified_at=datetime.now(timezone.utc).isoformat())
    (root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict))
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
