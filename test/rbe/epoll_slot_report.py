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
    r["observations"] = [line.strip() for line in log.splitlines() if "epoll MOD change-mask" in line or "watch struct=" in line]
    r["diagnostics"] = [line.strip()[:320] for line in log.splitlines() if any(x in line for x in ("--- FAIL", "panic:", "Error:", "expected", "want ", "unrecognized"))][:20]
    assert r["status"] == "PASSED" and int(code) == 0 and all(v == "pass" for v in r["cases"].values())
    names = set(r["cases"])
    if target.endswith(":vfs_test"):
        assert {"TestEpollFailedModify/pending_edge", "TestEpollFailedModify/delivered_edge", "TestEpollFailedModify/disarmed_one_shot", "TestEpollRestoreOneShot/armed", "TestEpollRestoreOneShot/disarmed", "TestEpollWatchSize"} <= names
        assert any("watch struct=" in x for x in r["observations"])
    if target.endswith(":epoll_contract_test"):
        assert {"TestModifiedEpollCheckpoint/one_change", "TestModifiedEpollCheckpoint/two_changes", "TestLinuxEpollModify", "TestEpollModifyAllocations"} <= names
        expected = {"false": 0, "true": int(lane == "old")}
        assert {k:float(v) for k,v in re.findall(r"epoll MOD change-mask=(false|true) allocations/call ([0-9.]+)",log)} == expected
    if lane=="fuse":assert names == {"TestEpollModifyAfterControlledAbort"}
    r["qualified"]=True
except Exception as err:
    r["error"] = f"{type(err).__name__} at line {err.__traceback__.tb_lineno}"
    r["build_diagnostics"] = [line.strip()[:450] for line in (out/"bazel.log").read_text().splitlines()
                               if "error:" in line or (line.startswith("ERROR:") and "--" not in line)][:10]
finally:
    (out/(target.split(":")[-1]+".json")).write_text(json.dumps(r,indent=2,sort_keys=True)+"\n")
    print(json.dumps(r,sort_keys=True))
