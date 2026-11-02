"""Verify the complete futex syscall suite on Linux and systrap."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

from qualification_report import xml_cases


def read(path):
    return path.read_text().strip() if path.is_file() else ""


def main():
    root = Path(sys.argv[1])
    fixture = Path("test/syscalls/linux/futex.cc")
    digest = sha256(fixture.read_bytes()).hexdigest()
    cases = set()
    for param, suite, name in re.findall(r"^TEST(_P)?\((\w+), (\w+)\) \{", fixture.read_text(), re.M):
        if param:
            assert suite == "PrivateAndSharedFutexTest"
            cases.update(f"SharedPrivate/{suite}.{name}/{i}" for i in range(2))
        else:
            cases.add(f"{suite}.{name}")
    assert len(cases) == 67 and digest == os.environ["FIXTURE"]
    lanes = {}
    for name, platform in (("current-native", "native"), ("current-guest", "runsc_systrap_directfs")):
        path = root / name
        xml = path / "test.xml"
        observed = []
        counts = {}
        seconds = None
        if xml.is_file() and xml.stat().st_size:
            top = ET.parse(xml).getroot()
            observed = [f"{t.get('classname')}.{t.get('name')}" for t in top.iter("testcase")]
            counts = xml_cases(xml)
            seconds = top.get("time")
        target = f"//test/syscalls:futex_test_{platform}"
        source = read(path / "source")
        rc = read(path / "exit")
        verified = (rc == "0" and source == os.environ["CURRENT"] and read(path / "target") == target
                    and read(path / "fixture-sha256").partition(" ")[0] == digest
                    and set(observed) == cases and len(observed) == len(cases)
                    and counts.get("passed") == len(cases) and counts.get("skipped") == 0)
        lanes[name] = dict(source=source, target=target, expected=len(cases), observed=len(observed),
                           missing=sorted(cases - set(observed)), counts=counts, xml_seconds=seconds,
                           executor=read(path / "executor"), started=read(path / "start"),
                           finished=read(path / "end"), exit=rc, verified=verified,
                           invocations=re.findall(r"Invocation ID: ([a-f0-9-]{36})",
                                                  read(path / "compiler-diagnostics")))
    owners = {}
    for name in ("futex_nogo", "futex_test"):
        path = root / "owners" / f"{name}.xml"
        label = f"//pkg/sentry/kernel/futex:{name}"
        counts = xml_cases(path) if path.is_file() and path.stat().st_size else {}
        ran = any(label in line and " PASSED " in line for line in read(root / "owners/receipt").splitlines())
        owners[label] = dict(counts=counts, verified=ran and counts.get("passed", 0) > 0
                             and counts.get("failed", 0) == 0)
    verified = (all(lane["verified"] for lane in lanes.values())
                and all(owner["verified"] for owner in owners.values())
                and read(root / "owners/exit") == "0")
    result = dict(lanes=lanes, owners=owners, source=os.environ["CURRENT"],
                  owners_finished=read(root / "owners/end"), verified=verified,
                  previous_controls=[13110426321, 13110665540],
                  verdict=datetime.now(timezone.utc).isoformat())
    (root / "verdict.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
