"""Exercise the original unlocked restore access and its guarded counterpart."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import previous


BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
PRODUCT = "cb20760ad91c04ac455401d8669737fc15530c7d"
TREE = "0494d025ad9dfd1b391b8430a12fa4f3c1a22e00"
WORK = Path(os.environ["RUNNER_TEMP"]) / "port-reservation"
OUT = WORK / "receipt"
DIR = Path("pkg/tcpip/transport/tcp")
RESTORE = DIR / "endpoint_state.go"
MIGRATION = DIR / "test/e2e/tcp_migration_test.go"
NOGO = "//pkg/tcpip/transport/tcp:tcp_nogo"
RUNTIME = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
CASES = ["TestTCPMigration", "TestRestoreListenWithPreexistingConnection",
         "TestSaveAfterRestoreWithReplaceConfig", "TestConnectAvoidsBoundPorts",
         "TestCloseWithPendingDataCwndFull", "TestClosedPortReservationCheckpoint"]
ARCHES = ["amd64", "arm64"]
END = time.monotonic() + 98 * 60
REPORT = {"base": BASE, "product": PRODUCT, "tree": TREE, "verified": False,
          "inputs": {}, "phases": {}, "errors": {}}


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def original(entry, dest, path):
    url = urlsplit(entry["uri"])
    assert url.scheme == "file" and not (url.netloc or url.query or url.fragment)
    real = Path(unquote(url.path))
    assert real.samefile(path) and 0 < real.stat().st_size <= 8 << 20
    data = real.read_bytes()
    assert not entry.get("digest") or entry["digest"] == sha(data)
    assert int(entry.get("length", len(data))) == len(data)
    dest.write_bytes(data)
    return {"bytes": len(data), "sha256": sha(data)}


def observe(phase, owner, rc, events):
    row = REPORT["phases"][phase]
    result_event, = [e for e in events if "testResult" in e]
    identity, result = result_event["id"]["testResult"], result_event["testResult"]
    assert identity["label"] == owner
    config = identity["configuration"]["id"]
    info = result.get("executionInfo", {})
    row.update(configuration=config, status=result["status"], strategy=info.get("strategy"),
               cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")), exit=rc)
    base = Path("bazel-testlogs") / owner.removeprefix("//").replace(":", "/")
    for name in ("test.log", "test.xml"):
        entry, = [f for f in result["testActionOutput"] if f["name"] == name]
        row[name] = original(entry, OUT / (phase + "." + name), base / name)
    finished, = [e["finished"]["exitCode"].get("code", 0) for e in events if "finished" in e]
    row["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
    completed, = [e["completed"] for e in events if "completed" in e
                  and (target := e["id"].get("targetCompleted", {})).get("label") == owner
                  and not target.get("aspect") and target["configuration"]["id"] == config]
    save()
    expected = 3 if phase == "unguarded" else 0
    assert rc == finished == expected and result["status"] == ("FAILED" if expected else "PASSED")
    if not expected:
        assert completed["success"]
    assert config and not row["cached"] and row["strategy"] in ("local", "standalone")
    assert all(int(identity.get(key, 1)) == 1 for key in ("run", "shard", "attempt"))
    text = (OUT / (phase + ".test.log")).read_text(errors="replace")
    xml = ElementTree.parse(OUT / (phase + ".test.xml"))
    if owner == NOGO:
        negative = phase == "unguarded"
        line, = [i + 1 for i, s in enumerate(RESTORE.read_text().splitlines())
                 if s == "\t\te.isPortReserved = false"]
        sections = sorted(re.findall(r"(?m)^(amd64|arm64)\.\.\.(PASS)?", text))
        assert sections == [(a, "" if negative else "PASS") for a in ARCHES], sections
        row["sections"] = {}
        for arch in ARCHES:
            src = Path("bazel-bin") / DIR / ("tcp_nogo." + arch + ".findings")
            assert src.stat().st_size <= 2 << 20
            dest = OUT / (phase + "." + arch + ".findings.json")
            shutil.copyfile(src, dest)
            findings = json.loads(dest.read_bytes()) or []
            row["sections"][arch] = {"count": len(findings), "sha256": sha(dest.read_bytes())}
            save()
            assert isinstance(findings, list) and bool(findings) == negative
            for f in findings:
                pos = f["Position"]
                assert f["Category"] == "checklocks" and f["GOOS"] == "linux" and f["GOARCH"] == arch
                assert Path(pos["Filename"]).name == RESTORE.name and pos["Line"] == line
                assert "invalid field access, mu (" in f["Message"]
                assert "must be locked when accessing isPortReserved (locks:" in f["Message"]
    else:
        cases = list(xml.iter("testcase"))
        assert cases and not any(c.find(k) is not None for c in cases for k in ("failure", "error", "skipped"))
        assert not any(c.get("status") in ("notrun", "suppressed") or c.get("result") in ("skipped", "suppressed") for c in cases)
        terminals = re.findall(r"(?m)^\s*--- PASS: (\S+) \(", text)
        names = {c.get("name", "") for c in cases}
        row["required"] = {}
        for case in CASES:
            row["required"][case] = {"tested": terminals.count(case),
                                     "xml": sorted(n for n in names if n == case or n.startswith(case + "/"))}
            assert terminals.count(case) == 1 and row["required"][case]["xml"], case
        marker, = re.findall(r"closed-port checkpoint bytes=(\d+) port=(\d+) loaded=Initial restored=Close rebound=true", text)
        assert int(marker[0]) > 0 and 0 < int(marker[1]) <= 65535
        row["closed_checkpoint"] = {"bytes": int(marker[0]), "port": int(marker[1])}
    row["verified"] = True


def run(phase, owner):
    row = REPORT["phases"][phase] = {"owner": owner, "verified": False, "attempts": []}
    extra = [] if owner == NOGO else ["--test_filter=^(" + "|".join(CASES) + ")$", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1"]
    for i in range(18):
        bep = WORK / (phase + "-" + str(i) + ".bep")
        args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
                "test", "--config=x86_64", "--jobs=4", "--lockfile_mode=off",
                "--remote_executor=", "--remote_cache=", "--bes_backend=",
                "--spawn_strategy=processwrapper-sandbox,local", "--strategy=TestRunner=local",
                "--test_sharding_strategy=disabled", "--nocache_test_results", "--runs_per_test=1",
                "--flaky_test_attempts=1", "--test_tag_filters=", "--build_tag_filters=",
                "--nobuild_event_json_file_path_conversion", "--build_event_json_file=" + str(bep),
                *["--distdir=" + str(d) for d in sorted(previous.DIRS)], *extra, owner]
        with (WORK / (phase + ".console")).open("w+b") as console:
            try:
                rc = subprocess.run(args, stdout=console, stderr=subprocess.STDOUT, timeout=max(1, END - time.monotonic())).returncode
            except subprocess.TimeoutExpired:
                rc = 124
            console.seek(0)
            text = console.read().decode(errors="replace")
        events = [json.loads(b) for b in bep.read_bytes().splitlines(keepends=True) if b.endswith(b"\n")] if bep.exists() else []
        row["attempts"].append({"exit": rc, "diagnostics": [s[:550] for s in text.splitlines() if s.startswith("ERROR:") or "Error downloading [" in s][-12:]})
        save()
        if any("testResult" in e for e in events):
            observe(phase, owner, rc, events)
            return
        if time.monotonic() >= END or not previous.recover(text):
            raise RuntimeError((owner, rc, row["attempts"][-1]["diagnostics"][-2:]))
    raise RuntimeError("exhausted public dependency recovery")


def main():
    previous.OUT = WORK
    previous.REPORT["assets"] = REPORT["inputs"]
    assert git("rev-parse", "HEAD", "HEAD^{tree}", "HEAD^").decode().splitlines() == [PRODUCT, TREE, BASE]
    assert git("hash-object", str(DIR / "endpoint.go")).decode().strip() == "3c5cf1ff9bdaf5b4260ab68fb9f32219cb4de5ab"
    good = git("show", PRODUCT + ":" + str(RESTORE))
    bad = git("show", BASE + ":" + str(RESTORE))
    assert RESTORE.read_bytes() == good
    transition = b"\t\te.isPortReserved = false\n\t\te.state.Store(uint32(StateClose))\n"
    assert bad.count(transition) == 1
    assert bad.replace(transition, b"\t\te.mu.Lock()\n" + transition + b"\t\te.mu.Unlock()\n") == good
    for phase in ("guarded", "unguarded", "runtime"):
        try:
            RESTORE.write_bytes(bad if phase == "unguarded" else good)
            if phase == "runtime":
                original_source = git("show", PRODUCT + ":" + str(MIGRATION))
                assert MIGRATION.read_bytes() == original_source
                MIGRATION.write_bytes(original_source + b"\n" + (WORK / "closed_port_restore.go").read_bytes())
            run(phase, RUNTIME if phase == "runtime" else NOGO)
        except Exception as error:
            REPORT["errors"][phase] = repr(error)[:1500]
        finally:
            RESTORE.write_bytes(good)
            save()
    REPORT["verified"] = not REPORT["errors"] and all(r["verified"] for r in REPORT["phases"].values())
    save()
    return 0 if REPORT["verified"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        save()
