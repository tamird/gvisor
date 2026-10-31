"""Check the readers, suite boundaries, and the two-pass fixture on both encoders."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import median
import sys

from qualification_report import xml_cases

root = Path(sys.argv[1])
issues = []
sources = {"stock": "STOCK", "stock_shift": "STOCK_SHIFT", "full": "SOURCE", "fixed_read": "SOURCE", "stock_read": "STOCK"}
results = {}
images = {}
required = {
    "stock": ["TestWindowInterop"],
    "stock_shift": ["TestGraphDeferredObjectDecode/multi_pass_round_trip"],
    "full": ["TestGraphDeferredObjectDecode/multi_pass_round_trip", "TestGraphGrowingWindows", "TestWindowInterop"],
    "fixed_read": ["TestWindowInterop"],
    "stock_read": ["TestWindowInterop"],
}

for arm, expected in sources.items():
    d = root / arm
    def read(key):
        p = d / key
        return p.read_text(errors="replace").strip() if p.is_file() else ""
    observed = {k: read(k) for k in ["source", "source-after", "start", "end", "exit"]}
    results[arm] = observed
    if observed["source"] != os.environ[expected] or observed["source-after"] != observed["source"] or observed["exit"] != "0" or not observed["start"] or not observed["end"]:
        issues.append(f"{arm}: source, time or exit")
    if not all((d / k).is_file() and not read(k) for k in ("source-before-status", "source-after-status")):
        issues.append(f"{arm}: dirty or absent source")
    want_targets = ["//pkg/state:state_test"]
    if arm == "full":
        want_targets.append("//pkg/state/tests:tests_test")
    targets = [x for x in read("argv").splitlines() if x.startswith("//")]
    if targets != want_targets:
        issues.append(f"{arm}: actual targets {targets}")
    observed["targets"] = targets
    for test in (["state", "generated"] if arm == "full" else ["state"]):
        try:
            xml = xml_cases(d / (test+".test.xml"))
        except (OSError, SyntaxError, ValueError) as e:
            xml = {"error": str(e)}
        expected_cases = required[arm] if test == "state" else ["TestGrowingSliceWindows/graph/new_tail", "TestGrowingSliceWindows/graph/later_merge"]
        log = read(test+".test.log")
        cases = re.findall(r"--- (PASS|FAIL|SKIP): (\S+)", log)
        skips = [t for v,t in cases if v == "SKIP"]
        passes = [t for v,t in cases if v == "PASS"]
        if xml.get("failed") != 0 or xml.get("passed",0) != len(passes) or xml.get("skipped") != len(skips) or not all(passes.count(t) == 1 for t in expected_cases) or any(v=="FAIL" for v,_ in cases):
            issues.append(f"{arm}/{test}: original XML or named cases")
        observed[test] = {"passed":len(passes), "skips":skips, "required":expected_cases, "xml":xml}
        if arm in ("fixed_read", "stock_read") and log.count("WINDOW-PEER decoded") != 1:
            issues.append(f"{arm}: peer not actually decoded")
        if test == "state" and arm != "stock_shift":
            m = re.findall(r"WINDOW-IMAGE hex=([0-9a-f]+)", log)
            if len(m) == 1:
                images[arm] = bytes.fromhex(m[0])
            else:
                issues.append(f"{arm}: no unique wire image")
        if arm == "full" and test == "state":
            m = re.findall(r"WINDOW-SAVE n=(\d+) order=(wide|narrow) sample=(\d+) save_ns=(\d+) bytes=(\d+)",log)
            expected_rows = {(str(n), o, str(i)) for n in (128, 256, 512, 1024, 2048) for o in ("narrow","wide") for i in range(6)}
            if len(m) != 60 or {(n,o,i) for n,o,i,*_ in m} != expected_rows:
                issues.append("full: synthetic sample grid incomplete")
            observed["warm"] = {}
            for n,o in sorted({(n,o) for n,o,*_ in m}):
                rows = [(int(ns),int(size)) for k,order,i,ns,size in m if k==n and order==o and i!="0"]
                if rows:
                    times, lengths = zip(*rows)
                    observed["warm"][n+"-"+o] = {"median_ns": median(times), "wire_bytes": sorted(set(lengths))}
# The package-wide invocation registers additional test types, so use the
# isolated runs for both peer checks. Their decoders validate the actual hash.
if not all(k in images for k in ("stock","fixed_read","stock_read")) or not (images["stock"] == images["fixed_read"] == images["stock_read"]):
    issues.append("isolated stock and corrected wire images differ")
result = {"ok": not issues, "errors":issues, "arms":results, "images":{k:{"bytes":len(v),"sha256":hashlib.sha256(v).hexdigest()} for k,v in images.items()},"verified_at":datetime.now(timezone.utc).isoformat()}
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps({"ok":not issues,"errors":issues,"images":result["images"]}))
sys.exit(bool(issues))
