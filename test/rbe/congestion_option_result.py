"""Keep the TCP_INFO comparison and original native result identities."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

OUT = Path(sys.argv[1])
CODE = int(sys.argv[2])
BASE = "ac7b4e0fdc7dc4674453438e5e15fc069895620c"
STOCK = "974a9300cfb62634d5295dc0b7291ef314093a5b"
FIX = "35792c6a401e1c57fcba5a2d38f0b6fb599399e3"
NATIVE = "//test/packetimpact/tests:tcp_rack_native_test"
NETSTACK = "//test/packetimpact/tests:tcp_rack_netstack_test"
SUITE = "TestCongestionControlSelection"
MODES = {f"{cc}_{mode}" for cc in ("cubic", "reno") for mode in ("none", "same", "other")}
EXPECTED = {SUITE} | {f"{SUITE}/{m}" for m in MODES}
ROW = re.compile(rb"CCA_SELECT from=(cubic|reno) mode=(none|same|other) to=(cubic|reno) mss=(\d+) initial=(\d+) before=(\d+)/(\d+) after=(\d+)/(\d+) state=(\d+)/(\d+) rto=(\d+)/(\d+) rtt=(\d+)/(\d+)")
FIELDS = "mss initial cwnd_before threshold_before cwnd_after threshold_after state_before state_after rto_before rto_after rtt_before rtt_after".split()
FRAGMENTS = ("not in post-loss congestion avoidance", "missing first segment retransmission", "retransmission state =", "TCP_CONGESTION changed the post-loss sender window", "TCP_CONGESTION =", "ExpectData", "panic:")

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

result = {"status": "INCOMPLETE", "child": None, "code": CODE, "owners": {}}
stage = "source"
try:
    rev = git("rev-parse", "HEAD")
    arm = git("rev-parse", "HEAD^")
    owners = os.environ["QUALIFICATION_TARGETS"].split()
    assert rev == os.environ["QUALIFICATION_COMMIT"] and arm in (STOCK, FIX)
    assert owners == ([NATIVE, NETSTACK] if arm == STOCK else [NETSTACK])
    assert git("rev-parse", "HEAD:test/packetimpact/tests/tcp_rack_test.go") == "0618b0e56bc3a4213a4e1c2b3d76c8794f64f6eb"
    assert not git("diff", "--name-only", arm, "HEAD", "--", "pkg", "test/packetimpact")
    result.update(carrier=rev, source=arm, base=BASE)
    events = [json.loads(line) for line in (OUT / "events.jsonl").open()]
    result["child"] = next(e["started"]["uuid"] for e in events if e.get("started", {}).get("uuid"))
    stage = "reports"
    for owner in owners:
        row = result["owners"][owner] = {}
        attempts = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == owner]
        assert len(attempts) == 1
        attempt = attempts[0]
        row.update(status=attempt["status"], remote=attempt.get("executionInfo", {}).get("strategy") == "remote",
                   cached=bool(attempt.get("cachedLocally") or attempt.get("executionInfo", {}).get("cachedRemotely")), reports={}, observations={})
        assert row["remote"] and not row["cached"]
        data = {}
        files = [f for f in attempt["testActionOutput"] if f["name"] in ("test.log", "test.xml")]
        assert {f["name"] for f in files} == {"test.log", "test.xml"} and len(files) == 2
        for f in files:
            name = f["name"]
            raw = Path("bazel-testlogs", owner[2:].replace(":", "/"), name).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            assert f.get("digest", digest) == digest and int(f.get("length", len(raw))) == len(raw)
            row["reports"][name] = {"sha256":digest, "bytes":len(raw)}
            data[name] = raw
        cases = ET.fromstring(data["test.xml"]).findall(".//testcase")
        row["cases"] = {c.attrib["name"]: "failed" if c.find("failure") is not None or c.find("error") is not None else ("skipped" if c.find("skipped") is not None else "passed") for c in cases}
        for m in ROW.finditer(data["test.log"]):
            cc, mode, selected = (x.decode() for x in m.groups()[:3])
            k = cc+"_"+mode
            r = dict(zip(FIELDS, map(int, m.groups()[3:]), selected=selected))
            assert k not in row["observations"] or r == row["observations"][k]
            row["observations"][k] = r
        row["clues"] = [s for s in FRAGMENTS if s.encode() in data["test.log"]]
    stage = "physical"
    raw_proc = subprocess.Popen([os.environ["QUALIFICATION_DECODER"], "print", "--raw=true", "--sort=false", "--compact_execution_log="+str(OUT/"exec.binpb")], stdout=subprocess.PIPE)
    records, files_by_id, tests = [], {}, []
    for line in raw_proc.stdout:
        records.append(line)
        if line.rstrip(b"\r\n") != b"}":
            continue
        entry = json.loads(b"".join(records)); records.clear()
        if "file" in entry and "id" in entry:
            files_by_id[entry["id"]] = entry["file"]
        spawn = entry.get("spawn", {})
        if spawn.get("mnemonic") != "TestRunner":
            continue
        props = {p["name"]:p["value"] for p in spawn["platform"]["properties"]}
        outputs = [files_by_id.get(o.get("outputId"), {}) for o in spawn.get("outputs", [])]
        tests.append({"action": spawn["digest"], "runner": spawn.get("runner"), "exit": spawn.get("exitCode",0), "cached": bool(spawn.get("cacheHit")), "isolation": props.get("workload-isolation-type"), "ipv6": props.get("network-enable-ipv6"), "image": props.get("container-image"), "xml": [f["digest"]["hash"] for f in outputs if f.get("path","").endswith("/test.xml")]})
    assert raw_proc.wait() == 0 and not records
    result["physical"] = tests
    stage = "selection"
    for label, r in result["owners"].items():
        phys, = (t for t in tests if t["xml"] == [r["reports"]["test.xml"]["sha256"]])
        assert phys["runner"] == "remote" and not phys["cached"] and phys["isolation"] == "firecracker" and phys["ipv6"] == "true" and phys["image"].endswith("cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a")
        assert set(r["cases"]) == EXPECTED and set(r["observations"]) == MODES
        for mode, obs in r["observations"].items():
            cc, method = mode.split("_")
            assert obs["selected"] == (cc if method != "other" else ("reno" if cc == "cubic" else "cubic"))
            assert obs["initial"] > obs["cwnd_before"] >= obs["threshold_before"] > 0 and obs["state_before"] == 0
            same = obs["cwnd_before"] == obs["cwnd_after"] and obs["threshold_before"] == obs["threshold_after"] and obs["state_before"] == obs["state_after"]
            assert (r["cases"][SUITE+"/"+mode] == "passed") == same
            if label == NATIVE or arm == FIX or method == "none":
                assert same
        assert (phys["exit"] == 0) == (r["status"] == "PASSED")
    result["status"] = "PASS" if CODE == 0 and all(r["status"] == "PASSED" for r in result["owners"].values()) else "FAIL"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as exc:
    result["error"] = {"stage":stage, "kind":type(exc).__name__, "line":exc.__traceback__.tb_lineno}
finally:
    (OUT/"result.json").write_text(json.dumps(result,sort_keys=True,indent=2)+"\n")
sys.exit(result["status"] != "PASS")
