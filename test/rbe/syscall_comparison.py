"""Check the named syscall results, including the expected stock failure."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from qualification_report import xml_cases


def read(path):
    return path.read_text().strip() if path.is_file() else ""


def failure_with_count(messages, expr, count):
    return any(expr in m and re.search(rf"\bActual:\s*{count}\b", m) for m in messages)


def different_owners(messages, expr):
    for message in messages:
        if expr in message:
            values = re.findall(r"Which is:\s*(\d+)", message)
            if len(values) == 2 and values[0] != values[1]:
                return True
    return False


def wrong_wake(messages):
    return (failure_with_count(messages, "futex_wake_bitset", 0)
            and failure_with_count(messages, "futex_wake(is_priv", 1)
            and different_owners(messages, "a.load() &")
            and any("futex_unlock_pi" in m and "errno=1 " in m for m in messages))


def original(root):
    with ZipFile(root.parent / "first.zip") as archive:
        v = json.loads(archive.read("verdict.json"))
        lanes = v["lanes"]
        failed = {case.get("name"): [failure.text or failure.get("message", "")
                                     for failure in case.findall("failure")]
                  for case in ET.fromstring(archive.read("stock-guest/test.xml")).iter("testcase")
                  if case.find("failure") is not None}
    return (v["owners_exit"] == "0" and all(v["owners"].values())
            and all(lanes[name]["verified"] and lanes[name]["counts"]["passed"] == 6
                    for name in ("current-native", "current-guest"))
            and lanes["stock-guest"]["source"] == "ec49b89b7bd76c8de045a6356f15713c7980333d"
            and lanes["stock-guest"]["counts"]["passed"] == 4
            and failed.keys() == {"PIWaiters/0", "PIWaiters/1"}
            and all(len(messages) == 4 and wrong_wake(messages) for messages in failed.values()))


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
        verified &= code == "3" and counts.get("failed") == 4
        for key in expected:
            messages = cases.get(key, [])
            if ".PIWaiters/" in key:
                verified &= (len(messages) == 5 and wrong_wake(messages)
                             and failure_with_count(messages, "0, 1, &a)", 0))
            else:
                verified &= (len(messages) == 5 and different_owners(messages, "word.load()")
                             and any("futex_trylock_pi" in m and "errno=11 " in m for m in messages)
                             and failure_with_count(messages, "syscall(", 0)
                             and failure_with_count(messages, "futex_wake(priv", 0)
                             and any("futex_unlock_pi" in m and "errno=1 " in m for m in messages))
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
    original_ok = original(root)
    verified = original_ok and all(result["verified"] for result in lanes.values()) and all(owners.values()) and read(root / "owners" / "exit") == "0"
    verdict = dict(original_artifact=13110426321, original_replayed=original_ok, lanes=lanes, owners=owners, owners_exit=read(root / "owners" / "exit"),
                   owners_finished=read(root / "owners" / "end"), verified=verified,
                   verified_at=datetime.now(timezone.utc).isoformat())
    (root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict))
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
