"""Retain only the named TCP test results, measured events and report identities."""
import hashlib
import json
import io
import zipfile
from urllib.parse import unquote,urlsplit
from first_loss_pcap import records, wire_result
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

out, code = Path(sys.argv[1]), int(sys.argv[2])
owner = "//test/packetimpact/tests:tcp_rack_native_test"
assert owner == os.environ["QUALIFICATION_TARGETS"]
suite = "TestPostLossIdleLinuxWindow"
modes = {f"{cc}_{p}" for cc in ("cubic","reno") for p in ("immediate","idle")}
expected = {suite, *(f"{suite}/{m}" for m in modes)}
product = "fe81d6830f886e54bf16ea539aeed8dd31f76eed"
fixture = os.environ["QUALIFICATION_COMMIT"]
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == fixture and git("merge-base",product,"HEAD") == product
    assert set(git("diff","--name-only",product,fixture,"--","pkg").splitlines()) == {"pkg/tcpip/transport/tcp/cubic.go","pkg/tcpip/transport/tcp/test/e2e/tcp_test.go"}
    assert git("diff","--name-only","059e6b9527","HEAD","--","test/packetimpact").splitlines() == ["test/packetimpact/tests/tcp_rack_test.go"]
    rows = [json.loads(line) for line in (out / "events.jsonl").open()]
    result["child"] = next((r["started"].get("uuid") for r in rows if "started" in r), None)
    stage = "BEP"
    attempts = [r for r in rows if r.get("id", {}).get("testResult", {}).get("label") == owner]
    result["attempts"] = len(attempts)
    result["failure_categories"] = sorted({reason if re.fullmatch(r"[A-Z_]{1,40}", reason) else "UNSPECIFIED" for r in rows if "aborted" in r for reason in [r["aborted"].get("reason", "UNSPECIFIED")]})
    assert len(attempts) == 1
    test = attempts[0]["testResult"]
    result["remote"] = test.get("executionInfo", {}).get("strategy") == "remote"
    result["cached"] = bool(test.get("cachedLocally") or test.get("executionInfo", {}).get("cachedRemotely"))
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
    result["diagnostics"] = [l.decode(errors="replace").strip()[:400] for l in content["test.log"].splitlines() if re.search(rb"(did not fill cwnd|bad full CA flight|primed window is not below IW|unexpected wire data|idle connection changed state|repair state=|not unconstrained congestion avoidance|connection failed to drain:|missing head repair:)",l)][:12]
    assert names == expected and len(cases) == len(expected)
    for line in content["test.log"].splitlines():
        if b"LINUX_IDLE_RESULT " not in line:
            continue
        r = json.loads(line.split(b"LINUX_IDLE_RESULT ", 1)[1])
        mode = r["Algorithm"] + "_" + r["Profile"]
        assert mode in modes and mode not in result["modes"] and re.fullmatch(r"[0-9a-zA-Z._+-]{1,120}", r["Kernel"])
        assert r["IdleNS"] == (30_000_000_000 if r["Profile"]=="idle" else 0)
        assert r["ActualIdleNS"] >= r["IdleNS"] and r["TotalRetrans"] == 1
        assert r["Window"] == 60000 and 0 < r["BaseThreshold"] <= r["PostLossCwnd"] < r["InitialCwnd"]
        assert 0 < r["PreThreshold"] <= r["PreCwnd"] < r["InitialCwnd"] and r["PreRTO"] > 0
        assert len(r["Flights"]) == 5 and r["Prime"]["Bytes"] == r["PostLossCwnd"]*r["MSS"]
        for f in [r["Prime"], *r["Flights"]]:
            assert f["Bytes"] == f["Captured"] == f["Unacked"] * r["MSS"] == f["Cwnd"] * r["MSS"] < r["Window"]
            assert f["Frames"] > 0 and 0 < f["RTT"] < f["RTO"]
        assert r["Prime"]["Unsent"] == 0 and all(f["Unsent"] >= 4*r["MSS"] for f in r["Flights"])
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
        if not zipfile.is_zipfile(io.BytesIO(body)): captures[sha] = body
        else:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                for item in z.infolist():
                    if item.filename.endswith(".pcap") and item.file_size < 8<<20:
                        data = z.read(item); captures[hashlib.sha256(data).hexdigest()] = data
        result.setdefault("capture_outputs", {})[sha] = {"bytes": len(body), "entries":len(captures)}
    parsed = {sha: records(data) for sha,data in captures.items()}
    for r in result["modes"].values():
        matches = [w for sha, rows in parsed.items() if (w := wire_result(rows,r,sha)) is not None]
        assert len(matches) == 1
        r["wire"] = matches[0]
    for cc in ("reno", "cubic"):
        a, b = (result["modes"][cc + "_" + p] for p in ("immediate", "idle"))
        result.setdefault("matched_preconditions", {})[cc] = all(a[k] == b[k] for k in
            ("MSS", "InitialCwnd", "PostLossCwnd", "PreCwnd", "PreThreshold", "BaseThreshold"))
    assert not code and result["test_status"] == "PASSED" and all(c["passed"] for c in result["cases"])
    # Original TestRunner output path+digest and its actual execution-group
    # properties distinguish the native worker from its compiler prerequisites.
    stage = "placement"
    files_by_id, tests, compilers = {}, [], []
    if (out / "exec.binpb").is_file():
        proc = subprocess.Popen([os.environ["QUALIFICATION_DECODER"],"print","--raw=true","--sort=false","--compact_execution_log="+str(out/"exec.binpb")],stdout=subprocess.PIPE)
        chunk = []
        for line in proc.stdout:
            chunk.append(line)
            if line.rstrip(b"\r\n") != b"}": continue
            entry=json.loads(b"".join(chunk)); chunk.clear()
            if "file" in entry and "id" in entry: files_by_id[entry["id"]]=entry["file"]
            spawn=entry.get("spawn",{}); mnemonic=spawn.get("mnemonic")
            if mnemonic != "TestRunner" and not (mnemonic == "CppCompile" and any(a.endswith("protobuf/text_format.cc") for a in spawn.get("args",[]))): continue
            props={p["name"]:p["value"] for p in spawn["platform"]["properties"]}
            row={"action":spawn["digest"],"runner":spawn.get("runner"),"cached":bool(spawn.get("cacheHit")),"exit":spawn.get("exitCode",0),
                 "isolation":props.get("workload-isolation-type"),"memory":props.get("EstimatedMemory"),"ipv6":props.get("network-enable-ipv6"),
                 "image":props.get("container-image")}
            if mnemonic == "TestRunner":
                row["xml"]=[f["digest"].get("hash") for item in spawn.get("outputs",[]) if (f:=files_by_id.get(item.get("outputId"),{})).get("path","").endswith("/test.xml")]
                tests.append(row)
            else: compilers.append(row)
        assert not chunk and proc.wait() == 0
    result["placement"]={"tests":tests,"protobuf_compilers":compilers}
    assert len(tests)==1 and tests[0]["runner"]=="remote" and not tests[0]["cached"] and tests[0]["exit"]==0
    assert tests[0]["isolation"]=="firecracker" and tests[0]["ipv6"]=="true" and tests[0]["xml"]==[result["reports"]["test.xml"]["sha256"]]
    assert tests[0]["image"].endswith("cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a")
    assert all(row["isolation"]=="firecracker" and row["memory"]=="8GB" for row in compilers)
    result["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage, "kind":type(err).__name__}
finally:
    for r in result["modes"].values():
        r.pop("RepairSeq",None)
        for f in [r["Prime"],*r["Flights"]]:
            f.pop("Start",None)
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
