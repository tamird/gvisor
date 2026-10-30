"""Project the real test result for each environment example; never publish raw logs."""
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
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
    rows = ET.fromstring(files["test.xml"]).findall(".//testcase")
    cases = {e.attrib["name"]: next((c.tag for c in e if c.tag in ("skipped","failure","error")), "pass") for e in rows}
    assert len(cases) == len(rows)
    r["cases"] = cases
    suite = "TestReviewGeneratedValues"
    required = {suite, suite+"/legacy", suite+"/graph", suite+"/legacy/flat", suite+"/legacy/nested", suite+"/legacy/order", suite+"/graph/flat", suite+"/graph/nested"}
    r["missing"] = sorted(required-set(cases))
    r["unexpected"] = sorted(set(cases)-required)
    outcomes = re.findall(rb"fixture=(nested|flat) engine=(legacy|graph) outer=(\d+) inner=(\d+)", files["test.log"])
    r["observations"] = {e.decode()+"/"+n.decode(): [int(v),int(i)] for n,e,v,i in outcomes}
    order = re.findall(rb"fixture=order engine=legacy input=(\d+) restored=(\d+)", files["test.log"])
    r["ordering"] = [list(map(int,row)) for row in order]
    r["matched"] = (set(cases)==required and all(v in ("pass","failure") for v in cases.values()) and len(outcomes)==4 and len(order)==1 and
                    (r["test_status"],r["bazel_exit"],int(code)) in (("PASSED",0,0),("FAILED",3,1)))
    r["verdict"] = "COMPLETE"
except Exception as exc:
    r.update(error=type(exc).__name__, line=exc.__traceback__.tb_lineno)
finally:
    (out / "result.json").write_text(json.dumps(r, indent=2, sort_keys=True)+"\n")
sys.exit(int(r["verdict"] != "COMPLETE" or not r.get("matched")))
