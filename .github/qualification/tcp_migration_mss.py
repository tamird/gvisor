"""Check the same five socket restores and the diagnostic ACK on each sender."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-migration-mss"
OUT = WORK / "results"
BASE = "01fa1742767266686dc5831217daadf8bcead9c3"
FIX = "93bdf65ef0c2179f59bf4a4b1ab6703c5eb7c8e5"
FILE = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go")
OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
TEST = "TestTCPMigrationPayloadSize"
CASES = {"incapable_to_capable", "incapable_to_capable_twice", "capable_to_capable_twice",
         "incapable_to_incapable_twice", "capable_to_incapable", "inflight_ack_credit"}
RESTORES = {"MigrationAllowed_RemoveConf_RouteExists", "MigrationDisabled_RemoveConf", "MigrationAllowed_RemoveConf_NoRoute"}
DIAGNOSTIC = "restored TCP payload = 1200, want 1196"


def git(*args):
    return subprocess.check_output(["git", *args])


def observe(arm, head, row, fixture):
    git("switch", "-q", "--detach", head)
    assert git("rev-parse", "HEAD").decode().strip() == head
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], check=True)
    original = FILE.read_bytes()
    bep = WORK / (arm + ".bep")
    stdout = WORK / (arm + ".log")
    try:
        FILE.write_bytes(fixture)
        args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", "test",
                "--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_executor=",
                "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
                "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
                "--strategy=TestRunner=local", "--nocache_test_results", "--test_sharding_strategy=disabled",
                "--flaky_test_attempts=1", "--test_timeout=360", "--test_arg=-test.v",
                "--test_filter=^(TestTCPMigration|TestTCPMigrationPayloadSize)$",
                "--build_event_json_file=" + str(bep), OWNER]
        with stdout.open("wb") as stream:
            row["exit"] = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT).returncode
    finally:
        FILE.write_bytes(original)
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
    assert ident["label"].removeprefix("@@") == OWNER and all(int(ident.get(k, 1)) == 1 for k in ("run", "attempt", "shard"))
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
    wanted = {TEST + "/" + c for c in CASES} | {"TestTCPMigration/" + c for c in RESTORES}
    observed = [(name, outcome) for outcome, name in terminals if name in wanted]
    assert len(observed) == len(wanted) and set(dict(observed)) == wanted
    cases = row["cases"] = dict(observed)
    wanted_fail = {TEST + "/" + n for n in ("incapable_to_capable", "incapable_to_capable_twice")} if arm == "stock" else set()
    for name, outcome in cases.items():
        case, = [c for c in xml.iter("testcase") if c.get("name") == name]
        failed = case.find("failure") is not None or case.find("error") is not None
        assert failed == (name in wanted_fail) and case.find("skipped") is None, name
        assert outcome == ("FAIL" if failed else "PASS"), (name, outcome)
    assert text.count(DIAGNOSTIC) == len(wanted_fail)
    assert (row["exit"], row["status"]) == ((3, "FAILED") if wanted_fail else (0, "PASSED"))
    matches = re.findall(r"MSS_CREDIT sent_packets=1 sent_bytes=1200 restored_mss=(\d+) cwnd_before=(\d+) cwnd_after=(\d+)", text)
    (mss, before, after), = matches
    assert int(mss) == (1200 if arm == "stock" else 1196) and int(after) > int(before)
    row["previous_packet"] = dict(bytes=1200, packets=1, restored_mss=int(mss), cwnd_before=int(before),
                                  cwnd_after=int(after), acknowledged_cwnd_increment=int(after)-int(before))
    row["verified"] = True


def main():
    carrier = os.environ["GITHUB_SHA"]
    assert git("rev-parse", carrier+"^").decode().strip() == FIX and git("rev-parse", FIX+"^").decode().strip() == BASE
    fixture = git("show", carrier + ":" + str(FILE))
    report = {"carrier": carrier, "stock": BASE, "candidate": FIX, "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
              "arms": {}, "verified": False}
    for arm, source in (("stock", BASE), ("candidate", FIX)):
        row = report["arms"][arm] = {"head": source, "verified": False}
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
