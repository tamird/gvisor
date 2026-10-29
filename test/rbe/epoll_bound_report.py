"""Check exact Bazel results before accepting a checkpoint or analyzer control."""
from datetime import datetime, timezone
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
    finished, = [e["finished"] for e in es if "finished" in e]
    ended = finished["exitCode"].get("code",0)
    r["execution_finished"] = finished.get("finishTime",finished.get("finishTimeMillis"))
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
        (out/(target.split(":")[-1]+"."+name)).write_bytes(raw)
    log = data["test.log"].decode(errors="replace")
    cases = ET.fromstring(data["test.xml"]).findall(".//testcase")
    r["cases"] = {x.get("name"): next((c.tag for c in x if c.tag in ("skipped","failure","error")), "pass") for x in cases}
    assert len(r["cases"]) == len(cases)
    for kind, name in re.findall(r"^\s*--- (PASS|FAIL|SKIP): (\S+) ", log, flags=re.MULTILINE):
        r["cases"].setdefault(name, {"PASS":"pass","FAIL":"failure","SKIP":"skipped"}[kind])
    r["observations"] = [line.strip() for line in log.splitlines() if "epoll MOD change-mask" in line or "watch struct=" in line]
    r["diagnostics"] = [line.strip()[:320] for line in log.splitlines() if "--- FAIL:" in line or "panic:" in line][:20]
    if lane == "stock":
        case = "TestOverlappingMaskDoesNotRepeatHostEdge"
        assert ended == 3 and r["status"] == "FAILED"
        assert r["cases"].get(case) == "failure"
        assert "unchanged watcher received the same host edge twice" in log
    else:
        assert ended == 0 and r["status"] == "PASSED"
        assert all(v == "pass" for v in r["cases"].values())
        label = target.split(":")[-1]
        cases = {"fdnotifier_test": ["TestRejectedMaskKeepsHostSubscription","TestOverlappingMaskDoesNotRepeatHostEdge"]}
        if not label.endswith("_nogo"):
            assert all(r["cases"].get(case) == "pass" for case in cases[label])
    r["qualified"]=True
except Exception as err:
    r["error"] = f"{type(err).__name__} at line {err.__traceback__.tb_lineno}"
    r["build_diagnostics"] = [line.strip()[:450] for line in (out/"bazel.log").read_text().splitlines()
                               if re.search(r"\.go:[0-9]+:",line) or "error:" in line or (line.startswith("ERROR:") and "--" not in line)][:10]
finally:
    r["verified_at"] = datetime.now(timezone.utc).isoformat()
    (out/(target.split(":")[-1]+".json")).write_text(json.dumps(r,indent=2,sort_keys=True)+"\n")
    print(json.dumps(r,sort_keys=True))
