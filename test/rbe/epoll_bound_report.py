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
        (out/(target.split(":")[-1]+"."+name)).write_bytes(raw)
    log = data["test.log"].decode(errors="replace")
    cases = ET.fromstring(data["test.xml"]).findall(".//testcase")
    r["cases"] = {x.get("name"): next((c.tag for c in x if c.tag in ("skipped","failure","error")), "pass") for x in cases}
    assert len(r["cases"]) == len(cases)
    for kind, name in re.findall(r"^\s*--- (PASS|FAIL|SKIP): (\S+) ", log, flags=re.MULTILINE):
        r["cases"].setdefault(name, {"PASS":"pass","FAIL":"failure","SKIP":"skipped"}[kind])
    r["observations"] = [line.strip() for line in log.splitlines() if "epoll MOD change-mask" in line or "watch struct=" in line]
    r["diagnostics"] = [line.strip()[:320] for line in log.splitlines() if "--- FAIL:" in line or "panic:" in line][:20]
    assert r["status"] == "PASSED" and all(v == "pass" for v in r["cases"].values())
    required = {"vfs_test":["TestEpollFailedRegistration","TestEpollFailedModify/pending_edge","TestEpollFailedModify/delivered_edge","TestEpollFailedModify/disarmed_one_shot","TestEpollRestoreOneShot/armed","TestEpollRestoreOneShot/disarmed","TestEpollWatchSize"],
                "fdnotifier_test":["TestRejectedMaskKeepsHostSubscription"],
                "transport_test":["TestFailedHostWaiterRegistration"],
                "proc_test":["TestOverlayEpollAdmission/direct_proc","TestOverlayEpollAdmission/overlay_regular","TestOverlayEpollAdmission/overlay_fifo"],
                "epoll_contract_test":["TestModifiedEpollCheckpoint/one_change","TestModifiedEpollCheckpoint/two_changes","TestLinuxEpollModify","TestEpollModifyAllocations","TestEpollEventfdPromotedToHost"]}
    label = target.split(":")[-1]
    if not label.endswith("_nogo"):
        assert all(n in r["cases"] for n in required[label]), (label, r["cases"])
    if label == "epoll_contract_test":
        assert {k:float(v) for k,v in re.findall(r"epoll MOD change-mask=(false|true) allocations/call ([0-9.]+)", log)} == {"false":0, "true":0}
    if label == "vfs_test":
        assert any("watch struct=" in line for line in r["observations"])
    r["qualified"]=True
except Exception as err:
    r["error"] = f"{type(err).__name__} at line {err.__traceback__.tb_lineno}"
    r["build_diagnostics"] = [line.strip()[:450] for line in (out/"bazel.log").read_text().splitlines()
                               if re.search(r"\.go:[0-9]+:",line) or "error:" in line or (line.startswith("ERROR:") and "--" not in line)][:10]
finally:
    (out/(target.split(":")[-1]+".json")).write_text(json.dumps(r,indent=2,sort_keys=True)+"\n")
    print(json.dumps(r,sort_keys=True))
