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
    suite = "TestGraphScratchLifetime"
    required = {suite} | {suite+"/"+n for n in ("reuse_plain","fresh_alias","reuse_alias","pool_only")}
    r["missing"] = sorted(required-set(cases))
    r["unexpected"] = sorted(set(cases)-required)
    text = files["test.log"].decode(errors="replace")
    traces = re.findall(r"SCRATCH roundtrip=1 reused=(true|false) self=(true|false) len=(\d+) cap=(\d+) stale=(true|false) alive=(true|false) after_clear=(true|false)", text)
    r["copies"] = [{"reused":rt=="true","self":al=="true","len":int(n),"cap":int(c),"stale":st=="true","alive":v=="true","after_clear":cl=="true"} for rt,al,n,c,st,v,cl in traces]
    pools = re.findall(r"POOL roundtrip=1 cache_first=(true|false) payload_first=(true|false) cache_second=(true|false) payload_second=(true|false)",text)
    r["pool"] = [dict(zip(("cache_first","payload_first","cache_second","payload_second"),(v=="true" for v in p))) for p in pools]
    r["matched"] = False
    if not r["missing"] and not r["unexpected"] and len(traces)==3 and len(pools)==1:
        x={(e["reused"],e["self"]):e for e in r["copies"]}
        controls = all(not e["after_clear"] for e in r["copies"]) and not any(x[v]["alive"] or x[v]["stale"] for v in ((True,False),(False,True)))
        u=x[True,True]
        q=r["pool"][0]
        observed=u["alive"] and u["stale"] and u["len"]==0 and u["cap"]>0
        expected_bad=set()
        if u["alive"] or u["stale"]: expected_bad.add(suite+"/reuse_alias")
        if q["payload_first"] or q["payload_second"]: expected_bad.add(suite+"/pool_only")
        if expected_bad: expected_bad.add(suite)
        native_ok=(r["test_status"],r["bazel_exit"],int(code))==(("FAILED",3,1) if expected_bad else ("PASSED",0,0))
        r["observed_stale"] = observed
        r["matched"] = controls and all(z["after_clear"]==False for z in [u]) and q["cache_first"] and not q["cache_second"] and not q["payload_second"] and q["payload_first"]==observed and (observed == (mode=="stock")) and all(cases[n]==("failure" if n in expected_bad else "pass") for n in cases) and native_ok
    r["verdict"] = "COMPLETE"
except Exception as exc:
    r.update(error=type(exc).__name__, line=exc.__traceback__.tb_lineno)
finally:
    (out / "result.json").write_text(json.dumps(r, indent=2, sort_keys=True)+"\n")
sys.exit(int(r["verdict"] != "COMPLETE" or not r.get("matched")))
