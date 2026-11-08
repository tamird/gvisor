"""Compare actual cumulative ACK and repair observations on two TCP senders."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-ack-ordinary"
OUT = WORK / "results"
BASE = "679d14eed77a4ce6029065d88248c8ce0902c270"
FIX = "71bf32380060634e1ec689aceca8e4baae8e14c3"
FILE = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go")
WIRE = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
UNIT = "//pkg/tcpip/transport/tcp:tcp_test"
TEST = "TestTCPMigrationMTUAckCredit"
PERMANENT = "TestTCPMigration/MigrationAllowed_SmallerMTUWithOutstandingPacket"
NEW = "TestCumulativeACKPacketBoundaries"
UNITFILE = Path("pkg/tcpip/transport/tcp/rack_test.go")
STEPS = {"smaller": [1200], "partial": [4, 1196, 1196, 1200, 1200], "two": [1204, 1204], "repair": [1196, 1200, 1200]}


def git(*args):
    return subprocess.check_output(["git", *args])


def observe(arm, head, row, fixture, owner):
    prefix = arm + ("-wire" if owner == WIRE else "-unit")
    path = FILE if owner == WIRE else UNITFILE
    leaves = ["repair"] if arm == "stock" else list(STEPS)
    pattern = ("^(TestTCPMigrationMTUAckCredit|TestTCPMigration)$/^(" + "|".join([PERMANENT.split("/")[1], *leaves]) + ")$") if owner == WIRE else "^" + NEW + "$"
    git("switch", "-q", "--detach", head)
    assert git("rev-parse", "HEAD").decode().strip() == head
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], check=True)
    original = path.read_bytes()
    bep = WORK / (prefix + ".bep")
    stdout = WORK / (prefix + ".log")
    try:
        path.write_bytes(fixture)
        args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", "test",
                "--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_executor=",
                "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
                "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
                "--strategy=TestRunner=local", "--nocache_test_results", "--test_sharding_strategy=disabled",
                "--flaky_test_attempts=1", "--test_timeout=360", "--test_arg=-test.v",
                "--test_filter=" + pattern,
                "--build_event_json_file=" + str(bep), owner]
        with stdout.open("wb") as stream:
            row["exit"] = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT).returncode
    finally:
        path.write_bytes(original)
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
    assert ident["label"].removeprefix("@@") == owner and all(int(ident.get(k, 1)) == 1 for k in ("run", "attempt", "shard"))
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
        (OUT / (prefix + "-" + name)).write_bytes(data)
        row["files"][name] = {"sha256": digest, "bytes": len(data)}
        docs[name] = data
    text = docs["test.log"].decode()
    xml = ET.fromstring(docs["test.xml"])
    terminals = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test\S+) \(", text)
    wanted = {TEST + "/" + case for case in leaves} | {PERMANENT} if owner == WIRE else {NEW}
    row["terminals"] = {name: state for state, name in terminals}
    for case in sorted(wanted):
        expected = "FAIL" if case == PERMANENT and arm == "stock" else "PASS"
        assert terminals.count((expected, case)) == 1, case
        node, = [n for n in xml.iter("testcase") if n.get("name") == case]
        assert node.find("skipped") is None and node.find("error") is None
        assert (node.find("failure") is not None) == (expected == "FAIL"), case
    if arm == "stock":
        assert row["exit"] == 3 and row["status"] == "FAILED"
        assert text.count("ACK of one old packet at smaller MSS: cwnd 10 -> 12, want 11") == 1
        assert {name for status, name in terminals if status == "FAIL"} == {PERMANENT, "TestTCPMigration"}
    else:
        assert row["exit"] == 0 and row["status"] == "PASSED"
    if owner == WIRE:
        armed, = re.findall(r"RTO_ARM offset=0 peer_byte=33 cwnd=(\d+) delay_ns=(\d+)", text)
        row["armed_cwnd"], row["rto_ns"] = map(int, armed)
        assert row["armed_cwnd"] == 10 and 0 < row["rto_ns"] <= 10**10
        steps = row["steps"] = {}
        for case in leaves:
            offsets = STEPS[case]
            matches = re.findall(rf"ACK_STEP case={case} index=(\d+) offset=(\d+) before=(\d+) after=(\d+) retrans=(\d+)", text)
            seq = steps[case] = [list(map(int, m)) for m in matches]
            assert [i for i, *_ in seq] == list(range(len(offsets))) and [x[1] for x in seq] == offsets
            size, mtu = 1196, 1236
            repair = case == "repair"
            marker = f"ACK_WIRE case={case} source_mtu=1500 mtu={mtu} first=1200 second={str(case == 'two').lower()} repair={str(repair).lower()} fresh={size} timeouts={int(repair)}"
            assert text.count(marker) == 1, (case, marker)
            assert all(b > 0 and a > 0 for _, _, b, a, _ in seq)
            if repair:
                assert seq[0][4] == 1 and seq[1][4] == 2 and seq[2][4] == 2
                assert seq[0][2:4] == [1, 2] and seq[2][2] == seq[2][3]
            else:
                assert all(x[4] == 0 for x in seq)
        if arm == "candidate":
            expected = {"smaller": [1], "partial": [0,0,0,1,0], "two": [2,0]}
            for case, deltas in expected.items():
                assert [a-b for _, _, b, a, _ in steps[case]] == deltas, case
    row["verified"] = True


def main():
    carrier = os.environ["GITHUB_SHA"]
    assert git("rev-parse", carrier+"^").decode().strip() == FIX and git("rev-parse", FIX+"^").decode().strip() == BASE
    fixtures = {owner: git("show", carrier + ":" + str(path)) for owner, path in [(WIRE, FILE), (UNIT, UNITFILE)]}
    report = {"carrier": carrier, "stock": BASE, "candidate": FIX, "fixtures_sha256": {k: hashlib.sha256(v).hexdigest() for k,v in fixtures.items()},
              "prior_artifact": 13624604339,
              "arms": {}, "verified": False}
    for arm, source in (("stock", BASE), ("candidate", FIX)):
        rows = report["arms"][arm] = {}
        for label in ((WIRE,) if arm == "stock" else (WIRE, UNIT)):
            row = rows[label] = {"head": source, "verified": False}
            try:
                observe(arm, source, row, fixtures[label], label)
            except Exception as exc:
                row["error"] = type(exc).__name__ + ": " + str(exc)[:350]
            (OUT / "result.json").write_text(json.dumps(report, indent=2))
    report["verified"] = all(r["verified"] for rows in report["arms"].values() for r in rows.values())
    (OUT / "result.json").write_text(json.dumps(report, indent=2))
    assert report["verified"]


if __name__ == "__main__":
    main()
