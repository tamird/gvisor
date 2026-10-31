"""Check the exact case in the submitted, reviewed source."""
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

root = Path(sys.argv[1])
arm = root / "correction"

def read(name):
    path = arm / name
    return path.read_text(errors="replace").strip() if path.is_file() else ""

target = "//pkg/sentry/fsimpl/eventfd:eventfd_test"
case = "TestNewFromHostRegistrationFailure"
args = read("argv").splitlines()
log = read("test.log")
results = re.findall(r"^--- (PASS|FAIL|SKIP): (\w+) \(", log, re.MULTILINE)
errors = []
if read("source") != os.environ["FIXED"] or read("source-after") != os.environ["FIXED"]:
    errors.append("source mismatch")
if read("product-sha256").partition(" ")[0] != "b272d572b1028444d561387a3f328ac549070824e94f24bc9b72107728f943bd" or not read("fixture-sha256"):
    errors.append("production or fixture fingerprint missing or changed")
if not read("start") or not read("end") or read("exit") != "0" or args.count(target)!=1 or args.count(f"--test_arg=-test.run=^{case}$")!=1:
    errors.append("target, time or exit mismatch")
if results != [("PASS", case)] or read("source-before-status") or read("source-after-status"):
    errors.append("did not execute exactly the reviewed case on a clean source")
try:
    xml = ET.parse(arm/"test.xml")
    if [t.get("name") for t in xml.iter("testcase")] != [case] or any(list(xml.iter(k)) for k in ("failure", "error", "skipped")):
        errors.append("missing or failed Bazel case")
except (OSError,ET.ParseError) as e:
    errors.append(str(e))
report = {"ok": not errors, "source":read("source"), "parent":read("parent"), "product":read("product-sha256"), "fixture":read("fixture-sha256"), "previous_control":"8cc75db84", "previous_fixed":"ce72c5b3b", "original_bundle":"13048386561", "target":target, "cases": results, "args":args, "rc":read("exit"), "start":read("start"), "end":read("end"), "verified":datetime.now(timezone.utc).isoformat(), "errors":errors}
(root/"verdict.json").write_text(json.dumps(report, indent=2)+"\n")
print(json.dumps(report))
sys.exit(bool(errors))
