"""Compare one ACK for data sent before IPv4 and IPv6 restore on the integrated TCP sender."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

import previous
import timer_teardown as recovery


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-migration-credit"
OUT = WORK / "results"
OWNER = previous.OWNER
FILE = previous.FILE
V4 = "TestTCPMigrationACKAfterMTUReduction"
V6 = "TestIPv6MigrationPayloadSize"
OLD = "inflight_ack_credit"
report = {"verified": False, "carrier": os.environ["GITHUB_SHA"], "prior_ipv6": 13640156885,
          "prior_ipv4": 13626101046, "public_inputs": recovery.seen}

def save():
    output = OUT / "partial"
    output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    output.replace(OUT / "result.json")

def original(event):
    ident = event["id"]["testResult"]
    result = event["testResult"]
    info = result.get("executionInfo", {})
    report.update(configuration=ident["configuration"]["id"], status=result["status"], strategy=info.get("strategy"), files={})
    docs = {}
    for item in result["testActionOutput"]:
        name = item["name"]
        if name not in ("test.xml", "test.log"):
            continue
        uri = urlsplit(item["uri"])
        assert uri.scheme == "file" and not uri.netloc
        path = Path(unquote(uri.path))
        assert 0 < path.stat().st_size <= 8 << 20
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert not item.get("digest") or item["digest"] == digest
        assert int(item.get("length", len(data))) == len(data) and name not in docs
        (OUT / name).write_bytes(data)
        docs[name] = data
        report["files"][name] = {"bytes": len(data), "sha256": digest}
    save()
    assert ident["label"].removeprefix("@@") == OWNER and all(int(ident.get(k,1))==1 for k in ("run","shard","attempt"))
    assert report["strategy"] in ("local","standalone") and not result.get("cachedLocally") and not info.get("cachedRemotely")
    return docs

def main():
    integrated = previous.git("rev-parse", "HEAD^").decode().strip()
    report["source"] = integrated
    previous.git("switch", "-q", "--detach", integrated)
    assert previous.git("rev-parse", "HEAD^{tree}").decode().strip() == "9366a726c8b088bb7245e613099efce4b12ec40f"
    assert not previous.git("diff", "HEAD", "--name-only")
    original_file = FILE.read_bytes()
    donor_file = previous.git("show", "fe8e3a2bd83cc3bee9995762569c7f40dfa13a8e:" + str(FILE))
    start, end = b"// TestIPv6MigrationPayloadSize ", b"// TestRestoreListenWithPreexistingConnection "
    assert donor_file.count(start) == donor_file.count(end) == 1
    excerpt = donor_file[donor_file.index(start):donor_file.index(end)]
    assert original_file.count(b"func "+V4.encode()+b"(") == 1 and V6.encode() not in original_file
    assert original_file.count(b'"gvisor.dev/gvisor/pkg/state"') == 1 and b'"gvisor.dev/gvisor/pkg/buffer"' not in original_file
    fixture = original_file.replace(b'"gvisor.dev/gvisor/pkg/state"', b'"gvisor.dev/gvisor/pkg/buffer"\n\t"gvisor.dev/gvisor/pkg/state"')+b"\n"+excerpt
    report["fixture_sha256"] = hashlib.sha256(fixture).hexdigest()
    report["unchanged_ipv6_functions_sha256"] = hashlib.sha256(excerpt).hexdigest()
    recovery.assets.DIST.mkdir(parents=True,exist_ok=True)
    recovery.known.update((r[2], r) for r in json.loads((WORK/"inputs.json").read_bytes()))
    bep = WORK / "events"
    log = WORK / "console"
    end_time = time.monotonic() + 39*60
    try:
        FILE.write_bytes(fixture)
        args = [str(WORK/"bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", "test",
                "--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_executor=",
                "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
                "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
                "--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=360",
                "--test_sharding_strategy=disabled", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v",
                "--test_filter=^("+V4+"|"+V6+")$/^"+OLD+"$", "--build_event_json_file="+str(bep)]
        for _ in range(12):
            bep.unlink(missing_ok=True)
            with log.open("wb") as output:
                rc=subprocess.run(args+["--distdir="+str(p) for p in sorted(recovery.dirs)]+[OWNER], stdout=output,
                                  stderr=subprocess.STDOUT, timeout=max(1,end_time-time.monotonic())).returncode
            events = [json.loads(line) for line in bep.read_bytes().splitlines() if line] if bep.exists() else []
            if any("testResult" in e for e in events) or not recovery.acquire(log.read_text(errors="replace")):
                break
    finally:
        FILE.write_bytes(original_file)
    report["exit"] = rc
    report["diagnostics"] = [l[:470] for l in log.read_text(errors="replace").splitlines() if l.startswith("ERROR:") or re.search(r"[.]go:\d+:\d+:",l)][-14:]
    save()
    result, = [e for e in events if "testResult" in e]
    docs = original(result)
    assert any((i:=e["id"].get("targetCompleted",{})).get("label","").removeprefix("@@")==OWNER and i.get("configuration",{}).get("id")==report["configuration"] for e in events)
    xml = ET.fromstring(docs["test.xml"])
    wanted = {V4, V6+"/"+OLD}
    names = {c.get("name") for c in xml.iter("testcase")}
    assert wanted <= names <= wanted|{V6} and not any(c.tag in ("failure","error","skipped") for c in xml.iter())
    text = docs["test.log"].decode()
    ends = re.findall(r"(?m)^\s*--- (PASS|SKIP|FAIL): (Test\S+) [(]",text)
    assert len(ends)==3 and {name for _,name in ends}==wanted|{V6} and all(state=="PASS" for state,_ in ends)
    assert text.count("ACK of 1200-byte packet at MSS 1196: cwnd 10 -> 11")==1
    assert text.count("MSS_CREDIT sent_packets=1 sent_bytes=1200 restored_mss=1192 cwnd_before=10 cwnd_after=11")==1
    previous.RESTORES={OLD:[True]}
    report["ipv6_wire"]=previous.verify_frames(text,"candidate")[OLD]
    report.update(ipv4={"mss":1196,"cwnd":[10,11]}, ipv6={"mss":1192,"cwnd":[10,11]})
    assert rc==0 and report["status"]=="PASSED"
    report["verified"]=True

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        report["error"]=repr(exc)[:400]
    save()
    raise SystemExit(not report["verified"])
