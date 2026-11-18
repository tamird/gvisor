"""Bind public reporting tests and preserve named canonical Bazel XML results."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

mode = sys.argv[1]
if mode == "bind":
    source, control = sys.argv[2:4]
    results = Path(sys.argv[4])
    texts = [subprocess.check_output(["git", "show", revision + ":test/benchmarks/tools/hey_test.go"], text=True) for revision in (source, control)]
    assert texts[0] == texts[1]
    (results / "public-test-binding.json").write_text(json.dumps({"source": source, "control": control, "identicalFullTestFile": True, "testFileSHA256": hashlib.sha256(texts[0].encode()).hexdigest()}, indent=2) + "\n")
    raise SystemExit(0)
logs, results = map(Path, sys.argv[2:4])
target = logs / "test/benchmarks/tools/tools_test"
destination = results / mode
destination.mkdir()
for name in ("test.log", "test.xml"):
    shutil.copy2(target / name, destination / name)
root = ET.parse(destination / "test.xml").getroot()
cases = list(root.iter("testcase"))
assert cases
skipped = [case.get("name") for case in cases if case.find("skipped") is not None]
errors = [case.get("name") for case in cases if case.find("error") is not None]
failed = {case.get("name") for case in cases if case.find("failure") is not None}
names = {case.get("name") for case in cases}
assert not skipped and not errors, (skipped, errors)
required = {"TestHeyReport/" + name for name in ("success", "notfound", "timeout", "emptyErrorSection")}
assert required <= names, names
raw = (destination / "test.log").read_text().replace("\x16", "")
if mode == "negative":
    assert {"TestHeyReport/timeout", "TestHeyReport/emptyErrorSection"} <= failed, failed
    assert not {"TestHeyReport/success", "TestHeyReport/notfound"} & failed, failed
    assert "Report completed: got true, want false" in raw
else:
    assert mode == "positive" and not failed, failed
    assert "TestHey" in names, names
(destination / "proof.json").write_text(json.dumps({"mode": mode, "cases": len(cases), "names": sorted(names), "failed": sorted(failed), "skipped": skipped, "errors": errors}, indent=2) + "\n")
