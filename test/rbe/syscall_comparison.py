"""Reconcile the two named syscall results with completed sandbox restores."""

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
    source = read(path / "source")
    target = read(path / "target")
    platform = "runsc_systrap_directfs_save"
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
    restores = sum("Executing:" in line and " restore " in line and "--image-path" in line
                   for line in read(path / "test.log").splitlines())
    verified = (source == os.environ["CURRENT"] and code == "0"
                and target == f"//test/syscalls:semaphore_test_{platform}"
                and fixture == os.environ["FIXTURE"] and set(cases) == expected
                and counts.get("skipped") == 0 and counts.get("passed") == len(expected)
                and not any(cases.values()) and restores > 0)
    diagnostics = read(path / "compiler-diagnostics")
    return dict(source=source, target=target, fixture=fixture, expected=sorted(expected),
                executed=sorted(cases), missing=sorted(expected - cases.keys()), counts=counts,
                failures={key: messages for key, messages in cases.items() if messages},
                restores=restores, exit=code, finished=read(path / "end"), verified=verified,
                invocations=sorted(set(re.findall(r"Invocation ID: ([a-f0-9-]{36})", diagnostics))))


def main():
    root = Path(sys.argv[1])
    result = lane(root, "current-save")
    verified = result["verified"]
    verdict = dict(verified=verified, lane=result, verified_at=datetime.now(timezone.utc).isoformat())
    (root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict))
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
