"""Check how many repairs occupied the first post-timeout congestion window."""
from hashlib import sha256
import json
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import unquote, urlparse
import sys
import xml.etree.ElementTree as ET

out, name, source, owner, status = sys.argv[1:]
out=Path(out)
r=dict(source=source, owner=owner, arm=name, command_exit=int(status), verdict="INCOMPLETE")
try:
    bep=[json.loads(s) for s in (out/"events.jsonl").read_text().splitlines()]
    r["invocation"]=next(e["started"]["uuid"] for e in bep if "started" in e)
    finished,=[e["finished"]["exitCode"] for e in bep if "finished" in e]
    r["bazel_exit"]=int(finished.get("code",0))
    r["bazel_exit_name"]=finished.get("name","")
    r["execution_finished"]=next(e["finished"].get("finishTime") or e["finished"].get("finishTimeMillis") for e in bep if "finished" in e)
    test,=[e["testResult"] for e in bep if e.get("id",{}).get("testResult",{}).get("label")==owner]
    r["test_status"]=test["status"]
    r["remote"]=test.get("executionInfo",{}).get("strategy")=="remote"
    r["cached"]=bool(test.get("cachedLocally") or test.get("executionInfo",{}).get("cachedRemotely"))
    assert r["remote"] and not r["cached"]
    r["reports"]={}
    by_name={e["name"]:e for e in test["testActionOutput"]}
    data={}
    for f in ("test.xml","test.log"):
        b=(Path("bazel-testlogs")/owner[2:].replace(":","/")/f).read_bytes()
        h=sha256(b).hexdigest()
        entry=by_name[f]
        if "digest" in entry:
            assert h==entry["digest"] and len(b)==int(entry.get("length",len(b)))
        else:
            uri=urlparse(entry["uri"])
            actual=Path("bazel-testlogs")/owner[2:].replace(":","/")/f
            if uri.scheme=="file":
                assert Path(unquote(uri.path)).resolve()==actual.resolve()
            else:
                assert uri.scheme=="bytestream" and uri.path.endswith("/"+h+"/"+str(len(b)))
        r["reports"][f]={"sha256":h,"bytes":len(b)}
        data[f]=b
    rows=ET.fromstring(data["test.xml"]).findall(".//testcase")
    cases={e.attrib["name"]:next((c.tag for c in e if c.tag in {"skipped","failure","error"}),"pass") for e in rows}
    assert len(rows)==len(cases)==3
    r["cases"]=cases
    suite = "TestRACKRecoveryAfterRTOTailLoss"
    modes = ("false", "true")
    wanted = {suite, *(suite + "/gso=" + mode for mode in modes)}
    assert set(cases) == wanted
    log = data["test.log"].decode(errors="replace")
    readings = re.findall(r"FIRST_FLIGHT gso=(false|true) cwnd=(\d+) repairs=(\d+) timeouts=(\d+)", log)
    r["first_flight"] = {mode: {"cwnd": int(w), "repairs": int(n), "timeouts": int(t)} for mode, w, n, t in readings}
    assert len(readings) == len(r["first_flight"]) == 2
    expected_count = {"proposal": 4, "single_credit": 5}[name]
    assert all(v == {"cwnd": 5, "repairs": expected_count, "timeouts": 1} for v in r["first_flight"].values())
    passed = name == "single_credit"
    assert all(s == ("pass" if passed else "failure") for s in cases.values())
    assert (r["test_status"] == "PASSED") == (r["bazel_exit"] == int(status) == 0) == passed
    if not passed:
        assert log.count("repairs after the ACK of #1 = 4, want 5") >= 2
    r["verdict"]="COMPLETE"
except Exception as exc:
    r["exception"]=type(exc).__name__
    r["line"]=exc.__traceback__.tb_lineno
finally:
    r["verdict_time"] = datetime.now(timezone.utc).isoformat()
    (out/"result.json").write_text(json.dumps(r,sort_keys=True,indent=2)+"\n")
sys.exit(int(r["verdict"]!="COMPLETE"))
