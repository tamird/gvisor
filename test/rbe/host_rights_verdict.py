"""Reconcile actual native and sandbox case results, then preserve timing."""
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
case = "GoferStreamTest.ReleaseUndeliveredHostRights"
arms = {}
errors = []
for arm in ("native", "master", "approved"):
    d = root / arm
    def read(key):
        path = d / key
        return path.read_text(errors="replace").strip() if path.is_file() else ""

    result = {k: read(k) for k in ("source", "start", "end", "exit", "fixtures", "product")}
    args = read("argv").splitlines()
    result["argv"] = args
    expected_source = os.environ["FIXED" if arm == "approved" else "CONTROL"]
    expected_target = "//test/syscalls:connect_external_test_" + ("native" if arm == "native" else "runsc_systrap_directfs")
    expected_flags = [("1", "3", "0"), ("0", "3", "0")] if arm == "native" else [("1", "1", str(int(arm == "approved"))), ("0", "1", str(int(arm == "approved")))]
    expected_hup = ("h", "o" if arm == "master" else "h")
    if result["source"] != expected_source or args.count(expected_target) != 1 or args.count("--test_env=GTEST_FILTER="+case) != 1 or not result["start"] or not result["end"]:
        errors.append(arm + ": incorrect provenance or target")
    if result["exit"] != ("3" if arm == "master" else "0"):
        errors.append(arm + ": unexpected Bazel exit")
    log = read("test.log")
    prefix = re.findall(r"HOST-RIGHTS peek=([01]) received=(\d+) ctrunc=([01])", log)
    hup = re.findall(r"HOST-RIGHTS prefix=(\w) suffix=(\w)", log)
    result.update(receives=prefix, pipe_observations=hup, cases=[])
    if prefix != expected_flags or hup != [expected_hup]:
        errors.append(arm + ": expected peek, consume and both pipe states")
    try:
        for c in ET.parse(d / "test.xml").iter("testcase"):
            children = [x for x in c if x.tag in ("failure","error")]
            skipped = c.find("skipped") is not None or c.get("status") == "notrun" or c.get("result") in ("skipped","suppressed")
            result["cases"].append({"name":c.get("classname","")+"."+c.get("name",""),"status":"skip" if skipped else "fail" if children else "pass","errors":[x.get("message") or x.text or "" for x in children]})
    except (OSError,ET.ParseError) as e:
        errors.append(f"{arm}: {e}")
    if [(c["name"],c["status"]) for c in result["cases"]] != [(case,"fail" if arm=="master" else "pass")]:
        errors.append(arm + ": missing or additional named case")
    # In the red arm require exactly the two short-import flags and the live
    # trailing writer. A failed accepted-prefix write/close invalidates it.
    if arm == "master":
        failures=[e for c in result["cases"] for e in c["errors"]]
        if len(failures) != 3 or sum("truncated" in e and "count < 3" in e for e in failures)!=2 or sum("state[1]" in e for e in failures)!=1:
            errors.append("master: unexpected failure diagnostics")
    arms[arm]=result
if len({r["fixtures"] for r in arms.values()}) != 1 or not arms["master"]["fixtures"] or arms["native"]["product"]!=arms["master"]["product"] or arms["master"]["product"]==arms["approved"]["product"]:
    errors.append("source fingerprints do not isolate the correction")
receipt={"ok":not errors,"source_base":"aa767e4c4","upstream_patch":"5b7fe075b","case":case,"arms":arms,"errors":errors,"verified_at":datetime.now(timezone.utc).isoformat()}
(root/"verdict.json").write_text(json.dumps(receipt,indent=2)+"\n")
print(json.dumps(receipt))
sys.exit(bool(errors))
