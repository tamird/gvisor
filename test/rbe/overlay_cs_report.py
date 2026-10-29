"""Check exact Bazel results before accepting a checkpoint or analyzer control."""
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

out, target, source, lane, code = sys.argv[1:]
out = Path(out)
r = dict(source=source, target=target, lane=lane, exit=int(code), qualified=False)
try:
    es = [json.loads(x) for x in (out / "events.jsonl").read_text().splitlines()]
    r["invocation"] = next(e["started"]["uuid"] for e in es if "started" in e)
    ended, = [e["finished"]["exitCode"].get("code", 0) for e in es if "finished" in e]
    assert ended == int(code)
    result, = [e["testResult"] for e in es if e.get("id",{}).get("testResult",{}).get("label") == target]
    r["status"] = result["status"]
    execution = result.get("executionInfo",{})
    assert execution.get("strategy") == "remote" and not execution.get("cachedRemotely") and not result.get("cachedLocally")
    r["reports"] = {}
    output = Path("bazel-testlogs") / target[2:].replace(":", "/")
    data = {}
    for name in ("test.log","test.xml"):
        raw = (output/name).read_bytes()
        dg = hashlib.sha256(raw).hexdigest()
        meta, = [f for f in result["testActionOutput"] if f["name"] == name]
        if "digest" in meta:
            assert dg == meta["digest"] and len(raw) == int(meta.get("length",len(raw)))
        else:
            uri = meta["uri"]
            assert uri.endswith("/"+dg+"/"+str(len(raw))) or uri.startswith("file:")
        r["reports"][name] = dict(hash=dg,size=len(raw))
        data[name] = raw
    log = data["test.log"].decode(errors="replace")
    cases = ET.fromstring(data["test.xml"]).findall(".//testcase")
    r["cases"] = {x.get("name"): next((c.tag for c in x if c.tag in ("skipped","failure","error")), "pass") for x in cases}
    assert cases and len(r["cases"]) == len(cases)
    r["restore"] = sum("Executing:" in line and " restore " in line and "--image-path" in line for line in log.splitlines())
    r["pass_marker"] = "OVERLAY_ROUNDTRIP: copied upper value; lower signaled restored watch; no signal after DEL" in log
    r["diagnostics"] = [line.strip()[:440] for line in log.splitlines()
                         if any(s in line for s in (": Failure","Expected:","  Actual:","Save failed:", "Error after restore","panic:","invalid field access", "must be locked", "must hold", "unexpected locks", "references leaked"))][:16]
    if lane in ("corrected","nosave"):
        actual = [v for k,v in r["cases"].items() if "OverlayCheckpointWaiter" in k]
        assert len(actual) == 1 and r["restore"] >= 1
        if lane == "corrected":
            assert r["pass_marker"] and actual == ["pass"] and int(code)==0 and r["status"] == "PASSED"
        else:
            assert not r["pass_marker"] and actual == ["failure"] and int(code)==3 and r["status"] == "FAILED"
            assert "sigtimedwait" in log and "EAGAIN" in log
    elif lane == "unlocked":
        assert int(code)==3 and r["status"] == "FAILED" and "registrations" in log and "regular_file.go" in log
    else:
        assert int(code)==0 and r["status"] == "PASSED" and set(r["cases"].values())=={"pass"}
    r["qualified"] = True
except Exception as err:
    r["error"] = f"{type(err).__name__} at line {err.__traceback__.tb_lineno}"
    r["build_diagnostics"] = [line.strip()[:450] for line in (out/"bazel.log").read_text().splitlines()
                               if "error:" in line or (line.startswith("ERROR:") and "--" not in line)][:10]
finally:
    (out/"result.json").write_text(json.dumps(r,indent=2,sort_keys=True)+"\n")
    print(json.dumps(r,sort_keys=True))
