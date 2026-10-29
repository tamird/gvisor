"""Retain only the named TCP test results, measured events and report identities."""
import hashlib
import json
import io
import zipfile
from urllib.parse import unquote,urlsplit
from first_loss_pcap import pcap_frames
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

out, code = Path(sys.argv[1]), int(sys.argv[2])
owner = "//test/packetimpact/tests:tcp_rack_native_test"
assert owner == os.environ["QUALIFICATION_TARGETS"]
suite = "TestApplicationLimitedLinuxWindow"
modes = {f"{cc}_{p}" for cc in ("cubic","reno") for p in ("dense","spaced")}
expected = {suite, *(f"{suite}/{m}" for m in modes)}
product = "fe81d6830f886e54bf16ea539aeed8dd31f76eed"
fixture = os.environ["QUALIFICATION_COMMIT"]
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == fixture and git("rev-parse", "HEAD^^") == product
    assert set(git("diff","--name-only",product,fixture,"--","pkg").splitlines()) == {"pkg/tcpip/transport/tcp/cubic.go","pkg/tcpip/transport/tcp/test/e2e/tcp_test.go"}
    assert git("diff","--name-only","HEAD^","HEAD","--","test/packetimpact").splitlines() == ["test/packetimpact/tests/tcp_rack_test.go"]
    rows = [json.loads(line) for line in (out / "events.jsonl").open()]
    stage = "BEP"
    attempts = [r for r in rows if r.get("id", {}).get("testResult", {}).get("label") == owner]
    result["attempts"] = len(attempts)
    result["failure_categories"] = sorted({reason if re.fullmatch(r"[A-Z_]{1,40}", reason) else "UNSPECIFIED" for r in rows if "aborted" in r for reason in [r["aborted"].get("reason", "UNSPECIFIED")]})
    assert len(attempts) == 1
    test = attempts[0]["testResult"]
    result["remote"] = test.get("executionInfo", {}).get("strategy") == "remote"
    result["cached"] = bool(test.get("cachedLocally") or test.get("executionInfo", {}).get("cachedRemotely"))
    result["child"] = next((r["started"].get("uuid") for r in rows if "started" in r), None)
    assert result["remote"] and not result["cached"]
    result["test_status"] = test["status"]
    stage = "reports"
    files = {f["name"]: f for f in test["testActionOutput"] if f["name"] in {"test.log", "test.xml"}}
    assert set(files) == {"test.log", "test.xml"}
    content = {}
    result["reports"] = {}
    for name, f in files.items():
        file = Path("bazel-testlogs", owner[2:].replace(":","/"), name)
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert f.get("digest", digest) == digest and int(f.get("length", len(data))) == len(data)
        content[name] = data
        result["reports"][name] = {"sha256": digest, "bytes": len(data)}
    cases = ET.fromstring(content["test.xml"]).findall(".//testcase")
    result["cases"] = [{"name": c.attrib["name"], "passed": all(c.find(t) is None for t in ("skipped", "error", "failure"))} for c in cases]
    stage = "linux"
    names = {c["name"] for c in result["cases"]}
    result["diagnostics"] = [l.decode(errors="replace").strip()[:400] for l in content["test.log"].splitlines() if re.search(rb"(repair state=|trickle (unacked|retransmits)=|burst bytes=|not unconstrained congestion avoidance|connection failed to drain:|missing head repair:|invalid burst payload)",l)][:12]
    assert names == expected and len(cases) == len(expected)
    for line in content["test.log"].splitlines():
        if b"LINUX_DEMAND_RESULT " not in line: continue
        r = json.loads(line.split(b"LINUX_DEMAND_RESULT ",1)[1])
        mode = r["Algorithm"]+"_"+r["Profile"]
        assert mode in modes and mode not in result["modes"]
        assert r["GapNS"] == (0 if r["Profile"] == "dense" else 40_000_000) and r["TotalRetrans"] == 1
        assert r["MSS"] > 0 and r["Window"] == 60000 and r["BurstBytes"] == r["BeforeBurstCwnd"]*r["MSS"] < r["Window"]
        assert r["Threshold"] <= r["PostLossCwnd"] and len(r["Samples"]) == 32
        assert all(v["Unacked"] == 1 and not v["NotSent"] and 0 < v["RTT"] < v["RTO"] for v in r["Samples"])
        result["modes"][mode] = r
    assert set(result["modes"]) == modes
    stage = "pcap"
    root = Path("bazel-testlogs", owner[2:].replace(":","/"))
    captures = {}
    for path in [*root.rglob("*.pcap"), *root.rglob("outputs.zip")]:
        if path.stat().st_size > 16<<20: continue
        body = path.read_bytes()
        sha = hashlib.sha256(body).hexdigest()
        advertisements = [f for f in test["testActionOutput"] if f.get("digest") == sha or sha in f.get("uri","").split("/") or unquote(urlsplit(f.get("uri","")).path).endswith("/"+str(path.relative_to(root)))]
        if not advertisements: continue
        assert all(int(f.get("length",len(body))) == len(body) for f in advertisements)
        if path.suffix == ".pcap": captures[sha] = body
        else:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                for item in z.infolist():
                    if item.filename.endswith(".pcap") and item.file_size < 8<<20:
                        data = z.read(item); captures[hashlib.sha256(data).hexdigest()] = data
        result.setdefault("capture_outputs", {})[sha] = {"bytes": len(body), "entries":len(captures)}
    parsed = {sha:pcap_frames(data) for sha,data in captures.items()}
    for r in result["modes"].values():
        head,burst,mss = r["RepairSeq"],r["BurstSeq"],r["MSS"]
        add = lambda v,n: (v+n) & 0xffffffff
        assert burst == add(head,37*mss)
        matches=[]
        for sha,frames in parsed.items():
            hits=[(i,f) for i,f in enumerate(frames) if f["seq"]==head and len(f["payload"])==mss]
            if len(hits)<2: continue
            p=(hits[0][1]["sport"],hits[0][1]["dport"])
            def same(f,ports): return (f["sport"],f["dport"])==ports
            sacks=[min(i for i,f in enumerate(frames) if same(f,p[::-1]) and f["ack"]==head and (add(head,mss),add(head,n*mss)) in f["sacks"]) for n in (2,3,4)]
            assert hits[0][0]<sacks[0]<hits[-1][0]
            last=min(i for i,f in enumerate(frames) if i>hits[-1][0] and same(f,p[::-1]) and f["ack"]==add(head,5*mss))
            for n in range(32):
                seq=add(head,(5+n)*mss)
                seg=min(i for i,f in enumerate(frames) if i>last and same(f,p) and f["seq"]==seq and len(f["payload"])==mss)
                last=min(i for i,f in enumerate(frames) if i>seg and same(f,p[::-1]) and f["ack"]==add(seq,mss))
            amount=0; count=0
            for f in frames[last+1:]:
                if same(f,p) and f["seq"]==add(burst,amount) and f["payload"]:
                    amount+=len(f["payload"]);count+=1
                    if amount>=r["BurstBytes"]:break
            assert amount == r["BurstBytes"]
            matches.append({"capture":sha,"repair_frames":len(hits),"sacks":len(sacks),"sparse_data_ack_pairs":32,"burst_bytes":amount,"burst_frames":count})
        assert matches
        r["wire"] = matches
    assert not code and result["test_status"] == "PASSED" and all(c["passed"] for c in result["cases"])
    result["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage, "kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
