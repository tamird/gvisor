"""Keep the original TestRunner exits, TCP_INFO values, and qualified case names."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET

CONTROL = "ee1936724518c929d1558d5db58c32d276391d16"
FIX = "b4275ff27964e2ef1f5f83888997dfebfcdcc97c"
BASE = "35792c6a401e1c57fcba5a2d38f0b6fb599399e3"
NATIVE = "//test/packetimpact/tests:tcp_rack_native_test"
GUEST = "//test/packetimpact/tests:tcp_rack_netstack_test"
UNIT = "//pkg/tcpip/transport/tcp:tcp_test"
WIRE = "TestCongestionControlAfterPeerFIN"
CREDIT = "TestCongestionControlSwitchACKCredit"
GROUPS = ("cubic_to_cubic", "reno_to_reno", "cubic_to_reno", "reno_to_cubic")
MATCH = re.compile(rb"CLOSE_SELECT from=(cubic|reno) to=(cubic|reno) state=(\d+) initial=(\d+) mss=(\d+) window=(\d+) threshold=(\d+)")
FIELDS = ("state", "initial", "mss", "window", "threshold")

def cases(xml: bytes, expected: set[str]) -> dict[str, str]:
    nodes = ET.fromstring(xml).findall(".//testcase")
    result = {n.attrib["name"]: ("failed" if n.find("failure") is not None or n.find("error") is not None else "skipped" if n.find("skipped") is not None else "passed") for n in nodes}
    assert len(nodes) == len(result) and result.keys() == expected, "unexpected XML cases"
    return result

def windows(log: bytes, expected=frozenset(GROUPS)) -> dict:
    found = {}
    for match in MATCH.finditer(log):
        initial, chosen = (s.decode() for s in match.groups()[:2])
        key = initial + "_to_" + chosen
        row = dict(zip(FIELDS, map(int, match.groups()[2:])))
        assert key not in found or found[key] == row, "contradictory TCP_INFO"
        found[key] = row
    assert found.keys() == expected, "missing TCP_INFO"
    return found

def preflight() -> None:
    assert cases(b'<testsuite><testcase name="control"/><testcase name="bad"><failure/></testcase></testsuite>',{"control","bad"}) == {"control":"passed","bad":"failed"}
    single = b"CLOSE_SELECT from=reno to=cubic state=8 initial=11 mss=500 window=7 threshold=7"
    assert windows(single, {"reno_to_cubic"})["reno_to_cubic"] == dict(state=8, initial=11, mss=500, window=7, threshold=7)
    for read, args in ((cases,(b'<testsuite/>',{"bad"})),(windows,(b"",{"reno_to_cubic"}))):
        try:
            read(*args)
        except AssertionError:
            continue
        raise ValueError("accepted missing test/marker")

def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()

def physical(out: Path) -> list[dict]:
    proc = subprocess.Popen([os.environ["QUALIFICATION_DECODER"],"print","--raw=true","--sort=false","--compact_execution_log="+str(out/"exec.binpb")],stdout=subprocess.PIPE)
    blob, files, spawns = [], {}, []
    for line in proc.stdout:
        blob.append(line)
        if line.rstrip(b"\r\n") != b"}": continue
        entry = json.loads(b"".join(blob)); blob.clear()
        if "file" in entry and "id" in entry: files[entry["id"]] = entry["file"]
        s = entry.get("spawn",{})
        if s.get("mnemonic") != "TestRunner": continue
        props = {p["name"]:p["value"] for p in s.get("platform",{}).get("properties",[])}
        outputs = [files.get(o.get("outputId"),{}) for o in s.get("outputs",[])]
        spawns.append({"label":s.get("targetLabel"),"action":s.get("digest"),"exit":int(s.get("exitCode",0)), "runner":s.get("runner"),"cached":bool(s.get("cacheHit")),
            "isolation":props.get("workload-isolation-type"),"ipv6":props.get("network-enable-ipv6"),"xml":[f["digest"]["hash"] for f in outputs if f.get("path","").endswith("/test.xml")]})
    assert proc.wait() == 0
    return spawns

def main() -> None:
    preflight()
    if sys.argv[1:] == ["--preflight"]: return
    out = Path(sys.argv[1]); code = int(sys.argv[2])
    result = {"status":"INCOMPLETE","bazel_exit":code,"owners":{}}
    path = out/"result.json"
    try:
        arm = git("rev-parse","HEAD^")
        targets = os.environ["QUALIFICATION_TARGETS"].split()
        assert arm in (CONTROL,FIX) and targets == ([NATIVE,GUEST] if arm == CONTROL else [GUEST])
        carrier = git("rev-parse","HEAD")
        assert os.environ["QUALIFICATION_COMMIT"] == carrier
        assert git("rev-parse","HEAD:test/packetimpact/tests/tcp_rack_test.go") == "62a18c1d30c19dcfe28b7034a5cd85beaa494e28"
        assert git("rev-parse","HEAD:pkg/tcpip/transport/tcp/cubic_test.go") == "3beddea7665bd94b2b7102aa66e2449b34160602"
        assert not git("diff","--name-only",arm,carrier,"--","pkg","test/packetimpact")
        result.update(source=arm,carrier=carrier,intermediate=CONTROL,first_candidate=BASE)
        events = [json.loads(line) for line in (out/"events.jsonl").open()]
        result["child"] = next(e["started"]["uuid"] for e in events if e.get("started",{}).get("uuid"))
        # Keep exits and BEP status even if a subsequent XML reader fails.
        spawns = physical(out)
        result["physical"] = spawns
        for label in targets:
            rec = result["owners"][label] = {}
            try:
                [attempt] = [e["testResult"] for e in events if e.get("id",{}).get("testResult",{}).get("label") == label]
                rec.update(status=attempt["status"], remote=attempt.get("executionInfo",{}).get("strategy") == "remote",
                    cached=bool(attempt.get("cachedLocally") or attempt.get("executionInfo",{}).get("cachedRemotely")))
                # Match primary outputs by name, regardless of supplemental artifacts.
                files = {f["name"]:f for f in attempt["testActionOutput"] if f["name"] in ("test.log","test.xml")}
                assert len(files) == 2
                buf = {}
                rec["reports"] = {}
                for n,f in files.items():
                    buf[n] = Path("bazel-testlogs",label[2:].replace(":","/"),n).read_bytes()
                    h = hashlib.sha256(buf[n]).hexdigest()
                    assert h == f.get("digest",h) and len(buf[n]) == int(f.get("length",len(buf[n])))
                    rec["reports"][n] = {"sha256":h,"bytes":len(buf[n])}
                [worker] = [s for s in spawns if rec["reports"]["test.xml"]["sha256"] in s["xml"]]
                rec["native_exit"] = worker["exit"]
                assert rec["remote"] and not rec["cached"] and worker["runner"] == "remote" and not worker["cached"]
                assert worker["isolation"] == "firecracker" and worker["ipv6"] == "true"
                suite = CREDIT if label == UNIT else WIRE
                subs = GROUPS[2:] if label == UNIT else GROUPS
                rec["cases"] = cases(buf["test.xml"], {suite,*[suite+"/"+k for k in subs]})
                ok = label == NATIVE or arm == FIX
                assert (rec["native_exit"] == 0) == ok and (rec["status"] == "PASSED") == ok
                assert rec["cases"][suite] == ("passed" if ok else "failed")
                for name in subs:
                    good = ok or name in GROUPS[:2]
                    assert rec["cases"][suite+"/"+name] == ("passed" if good else "failed")
                if label != UNIT:
                    w = rec["observations"] = windows(buf["test.log"])
                    assert w["cubic_to_cubic"]["threshold"] != w["reno_to_reno"]["threshold"]
                    for name,row in w.items():
                        cc,sel = name.split("_to_")
                        assert row["state"] == 8 and row["mss"] > 0 and 0 < row["threshold"] <= row["window"] < row["initial"]
                        actual = sel if ok else cc
                        assert row["threshold"] == w[actual+"_to_"+actual]["threshold"]
                elif not ok:
                    rec["credit_diagnostics"] = {k:list(map(int,m.groups())) for k,r in (
                        ("cubic_to_reno",rb"first Reno ACK: window/credit = (\d+)/(\d+), want (\d+)/1"),
                        ("reno_to_cubic",rb"new CUBIC window/credit = (\d+)/(\d+), want (\d+)/3")) if (m:=re.search(r,buf["test.log"]))}
                    assert rec["credit_diagnostics"].keys() == set(subs)
            except Exception as e:
                rec["error"] = {"type":type(e).__name__,"line":traceback.extract_tb(e.__traceback__)[-1].lineno}
            path.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
        assert all("error" not in r for r in result["owners"].values())
        result["status"] = "PASS" if code == 0 else "FAIL"
    except Exception as e:
        result["error"] = {"type":type(e).__name__,"line":traceback.extract_tb(e.__traceback__)[-1].lineno}
    finally:
        path.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    sys.exit(result["status"] != "PASS")

if __name__ == "__main__":
    main()
