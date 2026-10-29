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
    suite = "TestLoadValueSnapshot"
    required = {suite, suite+"/legacy", suite+"/graph"}
    r["missing"] = sorted(required - set(cases))
    r["unexpected"] = sorted(set(cases) - required)
    rows = re.findall(rb"engine=(legacy|graph) field=([0-9]+) copied=([0-9]+) retained=([0-9]+)", files["test.log"])
    r["observations"] = {engine.decode(): list(map(int, values)) for engine,*values in rows}
    def match_native(failure):
        return (t["status"],r["bazel_exit"],int(code)) == (("FAILED",3,1) if failure else ("PASSED",0,0))
    stock = mode == "stock"
    r["matched"] = (
        mode in {"stock","fixed"} and set(cases)==required and len(rows)==2 and
        cases[suite+"/legacy"] == "pass" and cases[suite+"/graph"] == cases[suite] == ("failure" if stock else "pass") and
        r["observations"] == {"legacy":[22,11,11], "graph":[22,11,22 if stock else 11]} and
        match_native(stock)
    )
    r["verdict"] = "COMPLETE"
except Exception as exc:
    r.update(error=type(exc).__name__, line=exc.__traceback__.tb_lineno)
finally:
    (out / "result.json").write_text(json.dumps(r, indent=2, sort_keys=True)+"\n")
sys.exit(int(r["verdict"] != "COMPLETE" or not r.get("matched")))
