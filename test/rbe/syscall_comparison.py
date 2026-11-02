"""Verify the composed futex count and mixed-waiter behavior on Linux and gVisor."""

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
    expected = set(json.loads((Path(os.environ["RUNNER_TEMP"]) / "futex_lock_cases.json").read_text()))
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
    verified = (source == os.environ["CURRENT"]
                and target == f"//test/syscalls:futex_test_{platform}"
                and fixture == os.environ["FIXTURE"]
                and set(cases) == expected and counts.get("skipped") == 0)
    verified &= code == "0" and counts.get("passed") == len(expected) == 69
    diagnostics = read(path / "compiler-diagnostics")
    return dict(source=source, target=target, fixture=fixture, expected=sorted(expected),
                executed=sorted(cases), missing=sorted(expected - cases.keys()), counts=counts,
                failures={key: messages for key, messages in cases.items() if messages},
                exit=code, finished=read(path / "end"), verified=verified,
                invocations=sorted(set(re.findall(r"Invocation ID: ([a-f0-9-]{36})", diagnostics))))


def main():
    root = Path(sys.argv[1])
    lanes = {name: lane(root, name) for name in ("current-native", "current-guest")}
    verified = all(result["verified"] for result in lanes.values())
    verdict = dict(lanes=lanes, original_run=41375952995, original_artifact=13112030565,
                   verified_at=datetime.now(timezone.utc).isoformat())
    owners = root / "owners"
    tests = sorted(re.findall(r"^func (Test\w+)\(t \*testing.T\)", Path("pkg/sentry/kernel/futex/futex_test.go").read_text(), re.M))
    passed = sorted(re.findall(r"^--- PASS: (Test\w+) \(", read(owners / "futex_test.log"), re.M))
    arches = sorted(re.findall(r"^(amd64|arm64)\.\.\.PASS\s*$", read(owners / "futex_nogo.log"), re.M))
    verified &= (len(tests) == 13 and tests == passed and arches == ["amd64", "arm64"]
                 and read(owners / "futex_test.exit") == read(owners / "futex_nogo.exit") == "0")
    probe = json.loads((root / 'lock-probe.json').read_text())
    verified &= probe['verified']
    verdict.update(verified=verified, lock_probe=probe, unitTests=passed, analyzerArches=arches,
                   owners_finished=read(owners / "end"))
    (root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict))
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
