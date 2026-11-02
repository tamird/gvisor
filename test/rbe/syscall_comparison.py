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
                and target == f"//test/syscalls:futex_test_{platform}"
                and fixture == os.environ["FIXTURE"]
                and set(cases) == expected and counts.get("skipped") == 0)
    if stock:
        mixed = {key for key in expected if ".PIWaiters/" in key}
        bad = {key for key, errors in cases.items() if errors}
        verified &= code == "3" and bad == mixed and len(mixed) == 2 and counts.get("passed") == 4
        for key in mixed:
            messages = cases.get(key, [])
            # Both wake forms must actually have returned a count, and the
            # locker must have resumed without owning (or being able to unlock)
            # the futex. Other failures cannot establish this result.
            for expr, count in (("futex_wake_bitset", 0), ("futex_wake(is_priv", 1)):
                verified &= any(expr in message and re.search(rf"\bActual:\s*{count}\b", message)
                                for message in messages)
            verified &= any("FUTEX_TID_MASK" in message for message in messages)
            verified &= any("futex_unlock_pi" in message and
                            ("Operation not permitted" in message or "EPERM" in message)
                            for message in messages)
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
    owners = {label: any(label in line and " PASSED " in line for line in receipt.splitlines())
              for label in ("//pkg/sentry/kernel/futex:futex_nogo", "//pkg/sentry/kernel/futex:futex_test")}
    verified = all(result["verified"] for result in lanes.values()) and all(owners.values()) and read(root / "owners" / "exit") == "0"
    verdict = dict(lanes=lanes, owners=owners, owners_exit=read(root / "owners" / "exit"),
                   owners_finished=read(root / "owners" / "end"), verified=verified,
                   verified_at=datetime.now(timezone.utc).isoformat())
    (root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict))
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
