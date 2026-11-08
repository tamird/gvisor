"""Compare actual cumulative ACK and repair observations on two TCP senders."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-ack-standalone"
OUT = WORK / "results"
BASE = "679d14eed77a4ce6029065d88248c8ce0902c270"
FIX = "74ce2c6b4884c4b6d30d7d8571da43f396de3f80"
FILE = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go")
WIRE = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
TEST = "TestTCPMigrationACKAfterMTUReduction"
EXISTING = ["TestTCPMigration/" + leaf for leaf in (
    "MigrationAllowed_RemoveConf_RouteExists", "MigrationDisabled_RemoveConf", "MigrationAllowed_RemoveConf_NoRoute")]


def git(*args):
    return subprocess.check_output(["git", *args])


def observe(arm, head, row, fixture):
    pattern = "^(TestTCPMigration|" + TEST + ")$"
    git("switch", "-q", "--detach", head)
    assert git("rev-parse", "HEAD").decode().strip() == head
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], check=True)
    original = FILE.read_bytes()
    bep = WORK / (arm + ".bep")
    stdout = WORK / (arm + ".log")
    try:
        if arm == "stock":
            FILE.write_bytes(fixture)
            assert git("diff", "--name-only", "HEAD").decode().splitlines() == [str(FILE)]
        else:
            assert original == fixture
        args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", "test",
                "--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_executor=",
                "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
                "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
                "--strategy=TestRunner=local", "--nocache_test_results", "--test_sharding_strategy=disabled",
                "--flaky_test_attempts=1", "--test_timeout=360", "--test_arg=-test.v",
                "--test_filter=" + pattern,
                "--build_event_json_file=" + str(bep), WIRE]
        with stdout.open("wb") as stream:
            row["exit"] = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT).returncode
    finally:
        if arm == "stock":
            FILE.write_bytes(original)
    git("diff", "--exit-code", "HEAD")
    row["console_sha256"] = hashlib.sha256(stdout.read_bytes()).hexdigest()
    events = [json.loads(line) for line in bep.open() if line.strip()]
    tests = [e for e in events if "testResult" in e["id"]]
    if len(tests) != 1:
        errors = [s[:360] for s in stdout.read_text(errors="replace").splitlines()
                  if "error" in s.lower() or ".go:" in s]
        row["diagnostics"] = errors[-10:]
        raise ValueError(f"{len(tests)} TestResult records")
    event, = tests
    ident, result = event["id"]["testResult"], event["testResult"]
    execinfo = result.get("executionInfo", {})
    assert ident["label"].removeprefix("@@") == WIRE and all(int(ident.get(k, 1)) == 1 for k in ("run", "attempt", "shard"))
    row.update(config=ident["configuration"]["id"], status=result["status"], strategy=execinfo.get("strategy"), files={})
    assert row["strategy"] in ("local", "standalone") and not result.get("cachedLocally") and not execinfo.get("cachedRemotely")
    docs = {}
    for item in result["testActionOutput"]:
        name = item["name"]
        if name not in ("test.log", "test.xml"):
            continue
        uri = urlsplit(item["uri"])
        assert uri.scheme == "file" and not uri.netloc
        path = Path(unquote(uri.path))
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert 0 < len(data) <= 8 << 20 and (not item.get("digest") or digest == item["digest"])
        assert int(item.get("length", len(data))) == len(data)
        (OUT / (arm + "-" + name)).write_bytes(data)
        row["files"][name] = {"sha256": digest, "bytes": len(data)}
        docs[name] = data
    text = docs["test.log"].decode()
    xml = ET.fromstring(docs["test.xml"])
    terminals = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test\S+) \(", text)
    cases = row["cases"] = {}
    for case in [*EXISTING, TEST]:
        expected = "FAIL" if arm == "stock" and case == TEST else "PASS"
        assert [(state, n) for state,n in terminals if n == case] == [(expected,case)], case
        node, = [n for n in xml.iter("testcase") if n.get("name") == case]
        assert node.find("skipped") is None and node.find("error") is None
        assert (node.find("failure") is not None) == (expected == "FAIL"), case
        cases[case] = expected
    assert {n for state,n in terminals if state != "PASS"} == ({TEST} if arm == "stock" else set())
    pair, = re.findall(r"ACK of 1200-byte packet at MSS 1196: cwnd (\d+) -> (\d+)", text)
    before, after = map(int, pair)
    row["wire"] = {"old_bytes": 1200, "restored_mss": 1196, "cwnd_before": before, "cwnd_after": after}
    finished = [e["finished"]["exitCode"].get("code",0) for e in events if "finished" in e]
    if arm == "stock":
        assert (before, after, row["exit"], row["status"], finished) == (10,12,3,"FAILED",[3])
        assert text.count("ACK of one old packet: got cwnd 12, want 11") == 1
    else:
        assert (before, after, row["exit"], row["status"], finished) == (10,11,0,"PASSED",[0])
    row["verified"] = True


def main():
    carrier = os.environ["GITHUB_SHA"]
    assert git("rev-parse", carrier+"^").decode().strip() == FIX and git("rev-parse", FIX+"^").decode().strip() == BASE
    fixture = git("show", FIX + ":" + str(FILE))
    prior = json.loads((OUT / "prior-replay.json").read_text())
    assert prior["archive_sha256"] == "a5c6933ee65aa98cff9ce106c66cdd1da6a2a52e96e1ed569f92e02e7183826a"
    assert prior["original_conclusion"] == "failure" and prior["replay_verified"]
    report = {"carrier": carrier, "stock": BASE, "candidate": FIX, "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
              "prior_replay_commit": "d12a58f58c341e4aca960e7d2db58e4fa8d087b1", "arms": {}, "verified": False}
    for arm, source in (("stock", BASE), ("candidate", FIX)):
        row = report["arms"][arm] = {"source":source, "verified":False}
        try:
            observe(arm, source, row, fixture)
        except Exception as exc:
            row["error"] = type(exc).__name__ + ": " + str(exc)[:350]
        (OUT / "result.json").write_text(json.dumps(report, indent=2))
    report["verified"] = all(row["verified"] for row in report["arms"].values())
    (OUT / "result.json").write_text(json.dumps(report, indent=2))
    assert report["verified"]


if __name__ == "__main__":
    main()
