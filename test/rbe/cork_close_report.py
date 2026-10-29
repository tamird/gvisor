"""Check one original remote TestResult and publish only case and time data."""
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

out, arm, source, label, root_name, exit_text = sys.argv[1:]
out = Path(out)
code = int(exit_text)
r = {"status": "INCOMPLETE", "arm": arm, "source": source, "owner": label, "native_exit": code}
modes = {"close", "shutdown", "uncork_then_close"}
try:
    rows = [json.loads(s) for s in (out / "events.jsonl").read_text().splitlines()]
    r["invocation"] = next(row["started"]["uuid"] for row in rows if "started" in row)
    targets = [row["testResult"] for row in rows if row.get("id", {}).get("testResult",{}).get("label")==label]
    assert len(targets) == 1
    test, = targets
    r["test_status"] = test["status"]
    r["remote"] = test.get("executionInfo",{}).get("strategy") == "remote"
    r["cached"] = bool(test.get("cachedLocally") or test.get("executionInfo",{}).get("cachedRemotely"))
    assert r["remote"] and not r["cached"]
    files = {f["name"]: f for f in test["testActionOutput"] if f["name"] in {"test.log","test.xml"}}
    assert set(files) == {"test.log","test.xml"}
    data, r["reports"] = {}, {}
    for name, f in files.items():
        b = (Path("bazel-testlogs")/label[2:].replace(":","/")/name).read_bytes()
        digest = hashlib.sha256(b).hexdigest()
        assert digest == f.get("digest",digest) and len(b) == int(f.get("length",len(b)))
        r["reports"][name] = {"sha256":digest,"bytes":len(b)}
        data[name] = b
    cases = ET.fromstring(data["test.xml"]).findall(".//testcase")
    by_name = {e.attrib["name"]: next((c.tag for c in e if c.tag in {"skipped","failure","error"}),"pass") for e in cases}
    assert set(by_name) == {root_name} | {root_name+"/"+mode for mode in modes} and len(cases)==4
    r["cases"] = by_name
    log = data["test.log"].decode(errors="replace")
    events = {}
    for mode,t1,t2,n in re.findall(r"CORK mode=(close|shutdown|uncork_then_close) data_ns=(-?[0-9]+) fin_ns=(-?[0-9]+) bytes=([0-9]+)",log):
        assert mode not in events
        events[mode] = {"data_ns":int(t1),"fin_ns":int(t2),"bytes":int(n)}
    for mode,ns,n in re.findall(r"CORK mode=(close|shutdown|uncork_then_close) read_to_eof_ns=([0-9]+) bytes=([0-9]+)",log):
        assert mode not in events
        events[mode] = {"to_eof_ns":int(ns),"bytes":int(n)}
    r["events"]=events
    assert set(events)==modes and all(x["bytes"]==len(b"corked payload") for x in events.values())
    if arm=="stock":
        assert code and test["status"]=="FAILED"
        assert {k for k,v in by_name.items() if v=="failure"}=={root_name,root_name+"/close",root_name+"/shutdown"}
        assert by_name[root_name+"/uncork_then_close"]=="pass"
        assert all(data["data_ns"] == data["fin_ns"] == (0 if mode=="uncork_then_close" else 200_000_000) for mode,data in events.items())
    else:
        assert not code and test["status"]=="PASSED" and set(by_name.values())=={"pass"}
        if arm=="fixed":
            assert all(x["data_ns"]==x["fin_ns"]==0 for x in events.values())
        else:
            assert arm=="linux" and all(0<x["to_eof_ns"]<200_000_000 for x in events.values())
    r["status"]="PASS"
except Exception as e:
    r["exception"]=type(e).__name__
    r["line"]=e.__traceback__.tb_lineno
finally:
    (out/"result.json").write_text(json.dumps(r,indent=2,sort_keys=True)+"\n")
sys.exit(int(r["status"]!="PASS"))
