"""Read the one original native TestResult, its reports and worker record."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

root = "TestGraphMapPublishesDirectInterface"
label = "//pkg/state:state_test"
out = Path(sys.argv[1])
events = [json.loads(line) for line in (out/"test.bep").open()]
[start] = [e["started"] for e in events if "started" in e]
[finished] = [e["finished"] for e in events if "finished" in e]
[test] = [e["testResult"] for e in events if e["id"].get("testResult",{}).get("label") == label]
result = {
    "source": "eb5036b67218924be2e8925f578eefe956525c0d",
    "upstream_source": "d09828cec993fd489eccabcff27aa68c024f2080",
    "native": start["uuid"],
    "finished": finished["finishTime"],
    "exit": int((out/"native-exit").read_text()),
    "status": test["status"],
    "reports": {}, "cases": {},
}
for filename in ("test.log", "test.xml"):
    [report] = [v for v in test["testActionOutput"] if v["name"] == filename]
    local = (out/filename).read_bytes()
    digest = sha256(local).hexdigest()
    uri = urlparse(report["uri"])
    assert uri.scheme == "bytestream" and uri.hostname == "remote.buildbuddy.io"
    assert uri.path.endswith(f"/blobs/{digest}/{len(local)}")
    if "digest" in report:
        assert report["digest"] == digest
    if "length" in report:
        assert int(report["length"]) == len(local)
    result["reports"][filename] = {"sha256": digest, "bytes": len(local)}
log = (out/"test.log").read_text()
xml = ET.parse(out/"test.xml")
for case in xml.iter("testcase"):
    name = case.attrib["name"]
    assert name not in result["cases"]
    status = "FAIL" if any(v.tag in ("error","failure") for v in case) else "SKIP" if any(v.tag=="skipped" for v in case) else "PASS"
    result["cases"][name] = status
[worker] = [json.loads(l) for l in (out/"worker.jsonl").open()]
props = {p["name"]:p["value"] for p in worker["platform"]["properties"]}
result["worker"] = {"platform": props, "runner": worker["runner"], "cache": worker.get("cacheHit",False), "exit": worker.get("exitCode",0)}
assert worker["runner"] == "remote" and not worker.get("cacheHit") and not test.get("cachedLocally")
assert props["Arch"]=="amd64" and props["OSFamily"]=="linux" and props["workload-isolation-type"]=="firecracker"
assert worker["targetLabel"] == label and worker["mnemonic"] == "TestRunner"
extra = "TestGraphMapHookEntryStorage"
expected = {root, root+"/map_value", root+"/map_pointer", root+"/interface_slot",
            extra, extra+"/restore", extra+"/inspect_without_callbacks"}
positive = (result["cases"] == dict.fromkeys(expected, "PASS")
            and result["status"] == "PASSED" and result["exit"] == worker.get("exitCode",0) == 0
            and log.count("hook_calls=1 points_to_token=true") == 3)
result["observation"] = "all original cases and both map storage cases passed" if positive else "unclassified"
result["verified"] = datetime.now(timezone.utc).isoformat()
(out/"summary.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
assert result["observation"] != "unclassified", result["cases"]
