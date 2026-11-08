"""Read the original TCP TestRunner and record the measured cumulative ACK credit."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
TEST = "TestTCPMigrationMTUAckCredit"
WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-ordinary-mtu"
OUT = WORK / "results"


def main(report):
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == BASE
    raw = WORK / "console"
    bep = WORK / "events.json"
    with raw.open("wb") as sink:
        report["exit"] = subprocess.run([
            str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", "test",
            "--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off",
            "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--strategy=TestRunner=local", "--nocache_test_results", "--test_sharding_strategy=disabled",
            "--test_timeout=360", "--flaky_test_attempts=1", "--test_arg=-test.v", "--test_filter=^"+TEST+"$",
            "--build_event_json_file="+str(bep), OWNER,
        ], stdout=sink, stderr=subprocess.STDOUT).returncode
    tests = []
    finished = []
    with bep.open() as src:
        for e in map(json.loads, src):
            if "testResult" in e["id"]:
                tests.append(e)
            if "finished" in e:
                finished.append(e["finished"]["exitCode"].get("code", 0))
    if len(tests) != 1:
        errors = [s[:320] for s in raw.read_text(errors="replace").splitlines() if "error" in s.lower() or ".go:" in s]
        report["diagnostics"] = errors[-12:]
        raise ValueError(f"{len(tests)} test results")
    event, = tests
    ident, result = event["id"]["testResult"], event["testResult"]
    assert ident["label"].removeprefix("@@") == OWNER and all(int(ident.get(k, 1)) == 1 for k in ("run", "attempt", "shard"))
    info = result.get("executionInfo", {})
    report.update(owner=OWNER, configuration=ident["configuration"]["id"], status=result["status"],
                  strategy=info.get("strategy"), cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")))
    docs = {}
    for item in result["testActionOutput"]:
        name = item["name"]
        if name not in ("test.xml", "test.log"):
            continue
        uri = urlsplit(item["uri"])
        assert uri.scheme == "file" and not uri.netloc
        data = Path(unquote(uri.path)).read_bytes()
        h = hashlib.sha256(data).hexdigest()
        assert 0 < len(data) <= 8 << 20 and (not item.get("digest") or item["digest"] == h)
        assert int(item.get("length", len(data))) == len(data)
        (OUT / name).write_bytes(data)
        report.setdefault("originals", {})[name] = {"bytes": len(data), "sha256": h}
        docs[name] = data
    assert report["strategy"] in ("local", "standalone") and not report["cached"]
    assert (report["exit"], report["status"], finished) == (0, "PASSED", [0])
    text = docs["test.log"].decode()
    xml = ET.fromstring(docs["test.xml"])
    report["cases"] = {}
    for leaf, mtu, size in [("same", 1500, 1200), ("smaller", 1236, 1196)]:
        case = TEST + "/" + leaf
        node, = [tc for tc in xml.iter("testcase") if tc.get("name") == case]
        assert not any(node.find(status) is not None for status in ("failure", "error", "skipped"))
        assert len(re.findall(r"(?m)^\s*--- PASS: "+case+r" \(", text)) == 1
        marker = rf"MTU_ACK old_mtu=1500 new_mtu={mtu} old_payload=1200 fresh_payload={size} restored_mss={size} cwnd_before=(\d+) cwnd_after=(\d+)"
        (before, after), = re.findall(marker, text)
        assert int(after) > int(before) > 0
        report["cases"][leaf] = dict(source_mtu=1500, destination_mtu=mtu, old_packets=1, old_bytes=1200,
                                     fresh_bytes=size, restored_mss=size, cwnd_before=int(before),
                                     cwnd_after=int(after), increment=int(after)-int(before))
    report["verified"] = True


if __name__ == "__main__":
    report = dict(upstream=BASE, carrier=os.environ["GITHUB_SHA"], verified=False)
    try:
        main(report)
    except Exception as exc:
        report["error"] = type(exc).__name__ + " " + str(exc)[:325]
        raise
    finally:
        (OUT / "result.json").write_text(json.dumps(report, indent=2))
