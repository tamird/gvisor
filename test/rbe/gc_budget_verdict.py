import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from qualification_report import xml_cases

d = Path(sys.argv[1])
out = {}
good = True
for variant, ref in (("stock", "STOCK"), ("corrected", "CURRENT")):
    row = d / variant
    xml = row / "test.xml"
    counts = xml_cases(xml) if xml.exists() else {"passed": 0, "skipped": 0, "failed": 0, "failures": []}
    names = {e.get("name", "") for e in ElementTree.parse(xml).iter("testcase")} if xml.exists() else set()
    want = {"TestVisibleCgroupGCBudget", *("TestVisibleCgroupGCBudget/" + n for n in ("parent", "grandparent", "leaf"))}
    log = (row / "test.log").read_text() if (row / "test.log").exists() else ""
    measured = [list(map(int, (parent, reported, heap))) + [leaf] for parent, leaf, reported, heap in
                re.findall(r"BUDGET parent=(\d+) leaf=(\d+|max) reported=(\d+) heap=(\d+)", log)]
    exp = {(536870912, 2147483648, 1073741824, "2147483648"), (536870912, 2**64 - 1, 1073741824, "max"), (536870912, 268435456, 134217728, "268435456")} if variant == "stock" else {(536870912, 536870912, 268435456, "2147483648"), (536870912, 536870912, 268435456, "max"), (536870912, 268435456, 134217728, "268435456")}
    failures = {n.split("TestVisibleCgroupGCBudget", 1)[-1] for n in counts["failures"]}
    rc = int((row / "exit").read_text())
    source = (row / "source").read_text().strip()
    observed = (not rc and not failures) if variant == "corrected" else (rc == 3 and {"/parent", "/grandparent"} <= failures <= {"", "/parent", "/grandparent"})
    ok = bool(observed and source == os.environ[ref] and names == want and not counts["skipped"] and len(measured) == 3 and set(map(tuple, measured)) == exp)
    out[variant] = dict(source=source, expected=sorted(want), executed=sorted(names), missing=sorted(want-names), counts=counts, bazel_exit=rc, matched=ok, budgets=measured)
    good &= ok
out.update(verdict="REPRODUCED_AND_FIXED" if good else "FAIL_OR_INCOMPLETE", execution_finished=(d / "end").read_text().strip(), verified=datetime.now(timezone.utc).isoformat(timespec="seconds"))
(d / "verdict.json").write_text(json.dumps(out, indent=2) + "\n")
print(out)
sys.exit(not good)
