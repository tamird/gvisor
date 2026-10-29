"""Check the original native result and the bounded fatal/error distinction."""
from hashlib import sha256
import json
from pathlib import Path
import sys

from matched_test_result import check_test, read_lanes
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

out, mode, source, owner, code = sys.argv[1:]
out = Path(out)
r = dict(source=source, mode=mode, owner=owner, command_exit=int(code), verdict="INCOMPLETE")
try:
    events = [json.loads(x) for x in (out / "events.jsonl").read_text().splitlines()]
    r["invocation"] = next(e["started"]["uuid"] for e in events if "started" in e)
    finished, = [e["finished"]["exitCode"] for e in events if "finished" in e]
    r["bazel_exit"] = finished.get("code", 0)
    t, = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == owner]
    r["test_status"] = t["status"]
    execinfo = t.get("executionInfo", {})
    r["remote"] = execinfo.get("strategy") == "remote"
    r["cached"] = bool(t.get("cachedLocally") or execinfo.get("cachedRemotely"))
    assert r["remote"] and not r["cached"]
    r["reports"] = {}
    files = {}
    for f in ("test.xml", "test.log"):
        item, = [x for x in t["testActionOutput"] if x["name"] == f]
        p = Path("bazel-testlogs") / owner[2:].replace(":", "/") / f
        data = p.read_bytes()
        h = sha256(data).hexdigest()
        if "digest" in item:
            assert item["digest"] == h and int(item.get("length", len(data))) == len(data)
        else:
            uri = urlparse(item["uri"])
            if uri.scheme == "file":
                assert Path(unquote(uri.path)).resolve() == p.resolve()
            else:
                assert uri.scheme == "bytestream" and uri.path.endswith("/"+h+"/"+str(len(data)))
        r["reports"][f] = dict(sha256=h, bytes=len(data))
        files[f] = data
        (out / f).write_bytes(data)
    rows = ET.fromstring(files["test.xml"]).findall(".//testcase")
    cases = {e.attrib["name"]: next((c.tag for c in e if c.tag in ("skipped","failure","error")), "pass") for e in rows}
    assert len(cases) == len(rows)
    r["cases"] = cases
    suite = "TestGraphRecursiveDescriptor"
    needle = "bounded child hit a runtime stack overflow in typeTable.add while saving a nil unregistered recursive slice"
    leaf = suite + "/unregistered_cycle"
    required = {suite} | {suite + "/" + name for name in (
        "type_info", "legacy_nil_root", "registered_cycle", "unregistered_acyclic", "unregistered_cycle")}
    failed = {suite, leaf} if mode == "stock" else set()
    aborted = [e["aborted"].get("description", "") for e in events if "aborted" in e]
    lanes = read_lanes(out)
    r["lane_exit"] = check_test(int(code), lanes, r["test_status"], rows, required, failed, aborted)
    assert r["lane_exit"] == r["bazel_exit"]
    diagnostic_present = needle in files["test.log"].decode()
    assert diagnostic_present == (mode == "stock")
    if mode == "stock":
        child, = [e for e in rows if e.attrib["name"] == leaf]
        assert needle in ET.tostring(child, encoding="unicode")
    r.update(witnessed_stack_overflow=diagnostic_present, verdict="COMPLETE")
except Exception as exc:
    r.update(error=type(exc).__name__, detail=str(exc)[:240], line=exc.__traceback__.tb_lineno)
finally:
    (out / "result.json").write_text(json.dumps(r, indent=2, sort_keys=True)+"\n")
sys.exit(int(r["verdict"] != "COMPLETE"))
