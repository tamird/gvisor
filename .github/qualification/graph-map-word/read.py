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
    "source": "b3509aa986616aaabd86f26d5c0ba8c5c07ebcff",
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
expected = {root:"FAIL", root+"/map_value":"FAIL", root+"/map_pointer":"PASS", root+"/interface_slot":"PASS"}
negative = "published pointer after the typed hook = <nil>, want the restored token 73"
xml_diagnostics = "\n".join(
    (entry.get("message") or "") + "\n" + "".join(entry.itertext())
    for case in xml.iter("testcase") if case.get("name") == root+"/map_value"
    for entry in case if entry.tag in ("failure","error")
)
result["observation"] = "unclassified"
if result["cases"] == expected and negative in log and negative in xml_diagnostics and result["status"]=="FAILED" and result["exit"]==3 and worker.get("exitCode",0) != 0:
    result["observation"] = "only the copied map word missed the hook"
elif result["cases"] == dict.fromkeys(expected,"PASS") and result["status"]=="PASSED" and result["exit"]==0 and not worker.get("exitCode",0):
    result["observation"] = "all three published values retained the hook"
result["verified"] = datetime.now(timezone.utc).isoformat()
(out/"summary.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
assert result["observation"] != "unclassified", result["cases"]
