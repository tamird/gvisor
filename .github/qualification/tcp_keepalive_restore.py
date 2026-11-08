"""Observe saved TCP keepalive deadlines with the actual native Bazel test owner."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from xml.etree import ElementTree

import netip_wildcard as harness
import previous


BASE = "12bdd02c2061dea8d6a252db59bd5d4b11e99e5f"
FIX = os.environ["FIX"]
WORK = Path(os.environ["RUNNER_TEMP"]) / "keepalive-restore"
OUT = WORK / "receipt"
OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
MIGRATION = "pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go"
TIMER = "pkg/tcpip/transport/tcp/timer.go"
NO_ROUTE = "TestTCPMigration/MigrationAllowed_RemoveConf_NoRoute"
LISTEN = "TestRestoreListenWithPreexistingConnection/EstablishedProbeConnection"
RESTORE = "TestRestoreKeepalive/"
DEADLINES = [RESTORE + n for n in ("idle", "between_probes", "disabled")]
REPORT = {"base": BASE, "product": FIX, "inputs": {}, "phases": {}, "errors": {}, "verified": False,
          "prior_untouched_restore": "https://github.com/tamird/gvisor/actions/runs/42516568976/artifacts/13631628379",
          "native_linux_live_keepalive": "https://github.com/tamird/gvisor/actions/runs/42518101675/artifacts/13633097991"}


def git(*args):
    return subprocess.check_output(["git", *args])


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def replace_once(s, before, after):
    assert s.count(before) == 1, before
    return s.replace(before, after)


def diagnostic(source):
    # The real migration test already holds the app endpoint across a failed
    # route lookup. Exercise its setsockopt after verifying it became StateError.
    a = "c.CreateConnected(testcontext.TestInitialSequenceNumber, 30000, -1 /* epRcvBuf */)"
    source = replace_once(source, a, a + "\n\t\t\tc.EP.SocketOptions().SetKeepAlive(true)")
    a = 't.Fatalf("Unexpected restored endpoint state: want %v, got %v", want, got)\n\t\t\t\t}'
    source = replace_once(source, a, a + """
                if !tc.hasRouteAfterRestore && got == tcp.StateError {
                    if !restoredEP.SocketOptions().GetKeepAlive() { t.Fatal("missing saved option") }
                    t.Log("KEEPALIVE_RESTORE route_missing_state_error")
                    idle := tcpip.KeepaliveIdleOption(time.Second)
                    if err := restoredEP.SetSockOpt(&idle); err != nil { t.Fatal(err) }
                    t.Log("KEEPALIVE_RESTORE closed_setsockopt_completed")
                }""")
    a = "// Try establishing a new connection to the restored listening socket."
    return replace_once(source, a, """listenEP.SocketOptions().SetKeepAlive(true)
                    if !listenEP.SocketOptions().GetKeepAlive() { t.Fatal("missing listener option") }
                    t.Log("KEEPALIVE_RESTORE listener_setsockopt_completed")
                    """ + a)


def record(phase, bep, owners):
    if not bep.exists():
        return set()
    events = [json.loads(line) for line in bep.open() if line.endswith("\n")]
    attempts = [(e["id"]["testResult"], e["testResult"]) for e in events if "testResult" in e]
    if not attempts:
        return set()
    (key, result), = attempts
    row = harness.REPORT
    assert owners == [OWNER] and key["label"] == OWNER
    config = key["configuration"]["id"]
    (complete,) = [e["completed"]["success"] if "success" in e["completed"] else False for e in events
                   if "completed" in e and (t := e["id"].get("targetCompleted"))
                   and not t.get("aspect") and (t["label"], t["configuration"]["id"]) == (OWNER, config)]
    row["test"] = {"owner": OWNER, "config": config, "status": result["status"], "completion":complete,
                   "strategy": result.get("executionInfo",{}).get("strategy"), "cached": result.get("cachedLocally",False),
                   "coordinates": [int(key.get(k,1)) for k in ("run","shard","attempt")]}
    data = {}
    for filename in ("test.log", "test.xml"):
        file, = [item for item in result["testActionOutput"] if item["name"] == filename]
        data[filename] = harness.contents(file, 20 << 20, "/testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_test/" + filename)
        name = row["phase"] + "." + filename.split(".")[1]
        (OUT/name).write_bytes(data[filename])
        row["test"][filename] = {"file":name, "sha256":harness.sha(data[filename]), "bytes":len(data[filename])}
    (row["invocation"],) = [e["started"]["uuid"] for e in events if "started" in e]
    save()
    assert config and not row["test"]["cached"] and row["test"]["strategy"] in ("standalone", "local")
    assert row["test"]["coordinates"] == [1,1,1]
    text = data["test.log"].decode(errors="replace")
    cases = list(ElementTree.fromstring(data["test.xml"]).iter("testcase"))
    assert cases and not any(c.find("skipped") is not None or c.get("status") == "notrun" for c in cases)
    row["failed"] = [c.get("name") for c in cases if any(c.find(k) is not None for k in ("failure","error"))]
    name = row["phase"]
    if name in ("stock", "fixed"):
        for label in DEADLINES:
            status = "FAIL" if name == "stock" and label != RESTORE+"disabled" else "PASS"
            assert label in {c.get("name") for c in cases} and ("--- "+status+": "+label+" ") in text, label
        if name == "stock":
            assert text.count("no probe at saved keepalive deadline") == 2
            assert set(row["failed"]) - {"TestRestoreKeepalive"} == set(DEADLINES[:2])
        else:
            for label in ("idle", "retransmit_oldest", "probe_closed_window", "probe_tail"):
                assert "--- PASS: TestRestoreSendTimer/"+label+" " in text
    else:
        assert "KEEPALIVE_RESTORE route_missing_state_error" in text
        if name == "without-cleanup":
            assert "panic: runtime error: invalid memory address" in text and "(*timer).enable" in text
            assert "KEEPALIVE_RESTORE closed_setsockopt_completed" not in text
        else:
            assert "--- PASS: "+NO_ROUTE+" " in text and "KEEPALIVE_RESTORE closed_setsockopt_completed" in text
            assert "KEEPALIVE_RESTORE listener_setsockopt_completed" in text and "--- PASS: "+LISTEN in text
    failed = name in ("stock","without-cleanup")
    assert result["status"] == ("FAILED" if failed else "PASSED")
    if not failed:
        assert complete and not row["failed"]
    row["observed"] = True
    save()
    return {OWNER}


def main():
    assert os.geteuid() != 0 and os.uname().machine == "x86_64"
    assert git("rev-parse", FIX+"^").decode().strip() == BASE
    permanent = git("show", FIX+":"+MIGRATION)
    diag = diagnostic(permanent.decode()).encode()
    previous.OUT = WORK
    previous.REPORT["assets"] = REPORT["inputs"]
    harness.WORK, harness.OUT, harness.record, harness.save = WORK, OUT, record, save
    for name, rev, pattern in (
        ("stock", BASE, "^TestRestoreKeepalive$"),
        ("fixed", FIX, "^(TestRestoreKeepalive|TestRestoreSendTimer)$"),
        ("closed-listener", FIX, "^(TestTCPMigration|TestRestoreListenWithPreexistingConnection)$/^(MigrationAllowed_RemoveConf_NoRoute|EstablishedProbeConnection)$"),
        ("without-cleanup", FIX, "^TestTCPMigration$/^MigrationAllowed_RemoveConf_NoRoute$"),
    ):
        cell = REPORT["phases"][name] = {"phase":name,"source":rev,"commands":{}}
        harness.REPORT = cell
        try:
            git("checkout","-q","--detach",rev)
            assert git("rev-parse","HEAD").decode().strip() == rev and git("diff","HEAD","--") == b""
            Path(MIGRATION).write_bytes(permanent if name in ("stock","fixed") else diag)
            if name == "without-cleanup":
                Path(TIMER).write_bytes(git("show",BASE+":"+TIMER))
            cell["migration_fixture"] = harness.sha(Path(MIGRATION).read_bytes())
            cell["timer_source"] = harness.sha(Path(TIMER).read_bytes())
            harness.run(name,[OWNER],["--test_env=GO_TEST_WRAP_TESTV=1","--test_arg=-test.v","--test_filter="+pattern],24 if name=="stock" else 8)
            terminal = [c for c in cell["commands"].values() if not c.get("recovered")]
            assert len(terminal)==1 and terminal[0]["exit"] == (3 if name in ("stock","without-cleanup") else 0)
            assert cell["observed"]
        except Exception as error:
            REPORT["errors"][name] = repr(error)[:850]
        finally:
            git("restore","--",MIGRATION,TIMER)
            save()
    REPORT["verified"] = not REPORT["errors"] and all(c.get("observed") for c in REPORT["phases"].values())
    save()
    return not REPORT["verified"]


if __name__ == "__main__":
    sys.exit(main())
