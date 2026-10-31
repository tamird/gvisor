import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from qualification_report import xml_cases

d = Path(sys.argv[1])
good = True
out = {}
for kind, ref in (("stock", "STOCK"), ("corrected", "CURRENT")):
    p = d / kind
    xml = p / "test.xml"
    counts = xml_cases(xml) if xml.is_file() else {"passed": 0, "failed": 0, "skipped": 0, "failures": []}
    names = {e.get("name") for e in ElementTree.parse(xml).iter("testcase")} if xml.is_file() else set()
    suite = "TestSandboxAncestorMemoryLimit"
    expected = {suite, *(f"{suite}/{name}" for name in ("larger-child", "unlimited-child", "smaller-child"))}
    failed = {f.split(suite, 1)[-1] for f in counts["failures"]}
    log = (p / "test.log").read_text() if (p / "test.log").exists() else ""
    readings = list(map(int, re.findall(r"guest MemTotal: (\d+) kB", log)))
    want = [2097152, 2097152, 262144] if kind == "stock" else [524288, 524288, 262144]
    # An uncapped guest may report more than 2 GiB on a larger host.
    measured = len(readings) == 3 and readings[0] == want[0] and readings[2] == want[2] and (readings[1] >= want[1] if kind == "stock" else readings[1] == want[1])
    rc = int((p / "exit").read_text())
    source = (p / "source").read_text().strip()
    status = (rc == 0 and not failed) if kind == "corrected" else (rc == 3 and {"/larger-child", "/unlimited-child"} <= failed <= {"", "/larger-child", "/unlimited-child"})
    ok = source == os.environ[ref] and names == expected and not counts["skipped"] and status and measured
    out[kind] = dict(source=source, target="//test/root:root_test", expected=sorted(expected), executed=sorted(names), missing=sorted(expected-names), counts=counts, bazel_exit=rc, memtotal_kib=readings, matched=ok)
    good &= ok
out.update(verdict="REPRODUCED_AND_FIXED" if good else "FAIL_OR_INCOMPLETE", execution_finished=(d/"end").read_text().strip(), verified=datetime.now(timezone.utc).isoformat(timespec="seconds"))
(d/"verdict.json").write_text(json.dumps(out, indent=2)+"\n")
print(out)
sys.exit(not good)
