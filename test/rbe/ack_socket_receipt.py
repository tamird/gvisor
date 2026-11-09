"""Observe the real socket-call control on Linux and the two proposal trees."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

WORK = Path(os.environ["RUNNER_TEMP"]) / "recvbuf"
OUT = WORK / "out"
OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_delayed_ack_test"
SOURCES = {"interactive": "f05cd8923fb8aa27186834df01d7433085db41f1",
           "auto": "b5062f2222f563af2f39c4cc2e57cd1194634146"}
PACKAGE = Path("pkg/tcpip/transport/tcp/test/e2e")
CONTEXT = Path("pkg/tcpip/transport/tcp/testing/context/context.go")


def save(phase: str, row: dict) -> None:
    dest = OUT / (phase + ".json")
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    tmp.replace(dest)


def setup(phase: str) -> dict[str, str]:
    clean = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True)
    assert not clean, clean
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCES[phase]
    go = (WORK / "ack_socket_packets.go.fixture").read_text()
    enable = ""
    build = PACKAGE / "BUILD"
    text = build.read_text()
    start = text.index('name = "tcp_delayed_ack_test",')
    end = text.index("\n)", start)
    rule = text[start:end]
    assert rule.count('"tcp_delayed_ack_test.go"') == 1
    insert = '"ack_socket_packets_test.go", "tcp_delayed_ack_test.go"'
    files = [PACKAGE / "ack_socket_packets_test.go", build, CONTEXT]
    if phase == "interactive":
        enable = """opt := tcpip.TCPDelayedACKOption(true)
            if err := c.Stack().SetTransportProtocolOption(tcp.ProtocolNumber, &opt); err != nil { t.Fatal(err) }"""
        insert = '"ack_socket_linux_test.go", ' + insert
        assert rule.count("deps = [") == 1
        rule = rule.replace("deps = [", 'deps = [\n        "@org_golang_x_sys//unix:go_default_library",')
        linux = PACKAGE / "ack_socket_linux_test.go"
        assert not linux.exists()
        linux.write_bytes((WORK / "ack_socket_linux.go.fixture").read_bytes())
        files.append(linux)
    else:
        go = go.replace('"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"', "")
    assert go.count("// ENABLE_PROPOSAL") == 1 and not files[0].exists()
    files[0].write_text(go.replace("// ENABLE_PROPOSAL", enable))
    build.write_text(text[:start] + rule.replace('"tcp_delayed_ack_test.go"', insert) + text[end:])
    text = CONTEXT.read_text()
    at = "synOpts := header.ParseSynOptions(tcpHdr.Options(), false /* isAck */)"
    assert text.count(at) == 1
    CONTEXT.write_text(text.replace(at, at + '\n\tc.t.Logf("WIRE_LOCAL_MSS %d", synOpts.MSS)'))
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def collect(phase: str, row: dict, bep: Path) -> None:
    for line in bep.open():
        event = json.loads(line)
        ident = event["id"]
        if "started" in event:
            row["invocation"] = event["started"]["uuid"]
        if "finished" in event and "buildFinished" in ident:
            row["bazel_status"] = event["finished"]["exitCode"]
        if "testSummary" in ident:
            assert ident["testSummary"]["label"] == OWNER
            row["summary"] = event["testSummary"]["overallStatus"]
        if "testResult" not in ident:
            continue
        target = ident["testResult"]
        assert target["label"] == OWNER and [target[k] for k in ("run","shard","attempt")] == [1,1,1] and "result" not in row
        result = event["testResult"]
        info = result["executionInfo"]
        row["result"] = {"id": target, "status": result["status"], "strategy": info.get("strategy"),
                         "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely")), "files": {}}
        log, xml = None, None
        for record in result["testActionOutput"]:
            if record["name"] not in {"test.log", "test.xml"}:
                continue
            url = urlsplit(record["uri"])
            assert url.scheme == "file" and not url.netloc
            path = Path(unquote(url.path)).resolve(strict=True)
            assert path.name == record["name"]
            assert path.parent.parts[-7:] == ("pkg","tcpip","transport","tcp","test","e2e","tcp_delayed_ack_test") and path.parent.parts[-8] == "testlogs"
            assert 0 < path.stat().st_size < 2 << 20
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            assert not record.get("digest") or record["digest"] == digest
            assert "length" not in record or int(record["length"]) == len(data)
            row["result"]["files"][record["name"]] = [len(data), digest]
            assert not re.search(rb'(?i)authorization\s*[:=]|github_pat_|gh[pousr]_', data)
            (OUT / (phase + "." + record["name"].split(".")[1])).write_bytes(data)
            if record["name"] == "test.log":
                log = data.decode()
                row["measurements"] = [s.strip()[:420] for s in log.splitlines()
                                       if re.search(r"\b(WIRE_LOCAL_MSS |ACK case=|LINUX set=|MSS peer=)",s)][:50]
            else:
                xml = ET.fromstring(data)
                row["cases"] = [(c.get("name"),[e.tag for e in c if e.tag in ("error","failure","skipped")]) for c in xml.iter("testcase")]
        assert log is not None and xml is not None
        save(phase,row)
    r = row["result"]
    assert row["summary"] == r["status"] == "PASSED" and r["strategy"] == "linux-sandbox" and not r["cached"]
    assert row["bazel_status"]["name"] == "SUCCESS" and row["exit"] == 0
    cases = {name for name,errors in row["cases"] if not errors}
    needed = {"TestACKSocketRequest/" + name for name in ("control","quickack","mss536","mss1460")}
    if phase == "interactive":
        needed.update("TestNativeACKSocketRequest/set" + flag + "_" + str(i) for flag in ("true","false") for i in range(4))
    assert needed <= cases and not any(errors for _,errors in row["cases"]), (needed,cases)
    marks = "\n".join(row["measurements"])
    assert len(re.findall(r"WIRE_LOCAL_MSS 536\b", marks)) == 4
    assert len(re.findall(r"ACK case=(?:control|quickack|mss536|mss1460) bytes=\d+ elapsed_stack_ms=(?:0|39|40)\b", marks)) == 4
    assert len(re.findall(r"LINUX set=", marks)) == (8 if phase == "interactive" else 0)


def main(phase: str) -> None:
    row = {"source": SOURCES[phase], "verified": False, "kernel": platform.release(), "arch":platform.machine()}
    save(phase,row)
    try:
        assert platform.machine()=="x86_64" and not (WORK/"auth").exists()
        row["fixture_sha256"] = setup(phase)
        save(phase,row)
        bep = WORK / (phase + ".bep")
        log = WORK / (phase + ".build")
        cmd = [str(WORK.parent/"bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
               "--config=x86_64", "--jobs=4", "--lockfile_mode=off", "--remote_cache=", "--remote_executor=", "--bes_backend=",
               "--distdir=" + str(WORK/"dist"), "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
               "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
               "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
               "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_filter=^Test(Native)?ACKSocketRequest$",
               "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
               "--test_timeout=120", "--build_event_json_file_path_conversion=false", "--build_event_json_file="+str(bep),OWNER]
        with log.open("w") as output:
            row["exit"] = subprocess.run(["timeout","-k","15s","27m",*cmd], stdout=output, stderr=subprocess.STDOUT).returncode
        if row["exit"]:
            row["diagnostics"] = [s[:480] for s in log.read_text(errors="replace").splitlines()
                                  if s.startswith("ERROR:") or re.search(r'ack_socket_\w+\.go:\d+:',s)][:12]
        save(phase,row)
        collect(phase,row,bep)
        row["verified"] = True
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {str(error)[:360]}"
        raise
    finally:
        save(phase,row)


if __name__=="__main__":
    main(sys.argv[1])
