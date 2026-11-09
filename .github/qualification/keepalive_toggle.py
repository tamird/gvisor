"""Run the same keepalive toggles on Linux, stock Netstack and corrected Netstack."""
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import zipfile
from xml.etree import ElementTree

import netip_wildcard as h
import previous
from packet_reader import parse_pcap

BASE = "ad55e4137a68d87deeb46518d93b063d8231a5d6"
FIX = "652abc17553ec414b91e8d8a9a17af53b109d466"
SOURCE = "test/packetimpact/tests/tcp_user_timeout_test.go"
CONNECT = "pkg/tcpip/transport/tcp/connect.go"
NATIVE = "//test/packetimpact/tests:tcp_user_timeout_native_test"
NETSTACK = "//test/packetimpact/tests:tcp_user_timeout_netstack_test"
MAIN = "TestKeepaliveReenable"
NAMES = [MAIN, MAIN + "BeforeDeadline", MAIN + "AfterFirstProbe"]
WORK = Path(os.environ["RUNNER_TEMP"]) / "keepalive"
OUT = WORK / "receipt" / "packets"
REPORT = {"base": BASE, "product": FIX, "kernel": platform.release(), "cells": {}, "commands": {}, "assets": {},
          "errors": {}, "verified": False}
ARM = None


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def replace_one(src, old, new):
    assert src.count(old) == 1, old
    return src.replace(old, new)


def fixture():
    data = git("show", FIX + ":" + SOURCE).decode()
    case = data[data.index("func " + MAIN + "("):data.index("func TestTCPUserTimeout(")]
    enable = "\tdut.SetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_KEEPALIVE, 1)\n"
    disable = "\tdut.SetSockOptInt(t, fd, unix.SOL_SOCKET, unix.SO_KEEPALIVE, 0)\n"
    receive = "\tif _, err := conn.ExpectData(t, &probe, &testbench.Payload{Bytes: []byte{}}, 4*time.Second); err != nil {\n"
    assert case.count(enable) == 2
    early = replace_one(case, MAIN+"(", NAMES[1]+"(")
    early = replace_one(early, enable+disable, "\tstart := time.Now()\n"+enable+disable)
    early = replace_one(early, "conn.Expect(t, probe, 3*time.Second)", "conn.Expect(t, probe, 100*time.Millisecond)")
    early = replace_one(early, receive, '\tif time.Since(start) >= time.Second {\n\t\tt.Fatal("original deadline elapsed during the control RPCs")\n\t}\n'+receive)
    after = replace_one(case, MAIN+"(", NAMES[2]+"(")
    after = replace_one(after, disable, receive+'\t\tt.Fatalf("missing first probe before disable: %v", err)\n\t}\n'+disable)
    # Leave the permanent test intact and append only these two timing controls.
    return (data + "\n" + early + after).encode()


def count_probes(raw):
    packets = parse_pcap(raw)
    starts = {}
    for pkt in packets:
        if pkt["flags"] == 2:
            key = (*pkt["route"], pkt["sport"], pkt["dport"], pkt["seq"])
            starts.setdefault(key, pkt)
    assert len(starts) == 3, len(starts)
    observations = []
    for src, dst, local, peer, seq in starts:
        incoming = [p for p in packets if p["route"] == (dst, src) and (p["sport"],p["dport"]) == (peer,local)]
        synseq, = {p["seq"] for p in incoming if p["flags"] == 18 and p["ack"] == (seq+1) % 2**32}
        probes = [p for p in incoming if p["flags"] == 16 and p["seq"] == synseq and p["ack"] == (seq+1) % 2**32 and not p["payload"]]
        observations.append({"count":len(probes), "timestamps":[p["timestamp"] for p in probes]})
    return observations



def collect(owner, result, key, row):
    cell = row["name"]
    status = "FAILED" if cell == "stock-netstack" else "PASSED"
    verdicts = ["FAIL", "PASS", "FAIL"] if status == "FAILED" else ["PASS"]*3
    docs = {}
    for item in result["testActionOutput"]:
        name = item["name"]
        if name not in ("test.log", "test.xml", "test.outputs__outputs.zip"):
            continue
        prefix = "/testlogs/" + owner[2:].replace(":", "/") + "/"
        path, data = h.contents(item, 32 << 20, None if name.endswith(".zip") else prefix + name)
        assert prefix in path.as_posix()
        docs[name] = data
        row["files"][name] = {"sha256": h.sha(data), "bytes": len(data)}
        if name in ("test.log", "test.xml"):
            (OUT / (cell+"."+name.rsplit(".",1)[1])).write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(docs["test.outputs__outputs.zip"])) as archive:
        name, = [n for n in archive.namelist() if n.endswith(".pcap")]
        assert 0 < archive.getinfo(name).file_size <= 8 << 20
        raw = archive.read(name)
    (OUT / (cell+".pcap")).write_bytes(raw)
    row["pcap"] = {"sha256":h.sha(raw), "bytes":len(raw)}
    log = docs["test.log"].decode(errors="replace")
    terms = re.findall(r"--- (PASS|FAIL|SKIP): (\S+) [(]", log)
    cases = list(ElementTree.fromstring(docs["test.xml"]).iter("testcase"))
    row["go"] = terms
    row["xml"] = {c.get("name"): ("FAIL" if any(c.find(k) is not None for k in ("error","failure"))
                   else "SKIP" if c.find("skipped") is not None else "PASS") for c in cases}
    row["diagnostics"] = [line.strip()[:280] for line in log.splitlines() if "tcp_user_timeout_test.go:" in line][:12]
    order = re.findall(r"=== RUN\s+(TestKeepaliveReenable(?:BeforeDeadline|AfterFirstProbe)?)\b", log)
    assert len(order) == 3 and set(order) == set(NAMES), order
    row["flows"] = dict(zip(order, count_probes(raw)))
    info = result.get("executionInfo",{})
    row.update(strategy=info.get("strategy"), cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")))
    assert result["status"] == status and row["strategy"] in ("local","standalone") and not row["cached"]
    assert all(int(key.get(n, 1)) == 1 for n in ("attempt","run","shard"))
    assert set(terms) == set(zip(verdicts, NAMES)) and len(terms) == len(cases) == 3
    assert row["xml"] == dict(zip(NAMES, verdicts))
    if status == "FAILED":
        assert log.count("keepalive probe after re-enable:") == 2
        assert [row["flows"][n]["count"] for n in NAMES] == [0, 1, 1]
    else:
        assert all(row["flows"][n]["count"] >= count for n,count in zip(NAMES, (1,1,2)))
    row["verified"] = True


def record(phase, bep, owners):
    seen = set()
    completions = {}
    if not bep.is_file():
        return seen
    with bep.open() as events:
        for line in events:
            if not line.endswith("\n"):
                break
            event = json.loads(line)
            ident = event["id"]
            if "started" in event:
                REPORT["commands"][phase]["invocation"] = event["started"].get("uuid")
            if "completed" in event and "targetCompleted" in ident:
                key = ident["targetCompleted"]
                if key["label"] in owners and not key.get("aspect"):
                    completions[key["label"]] = (key["configuration"]["id"], event["completed"].get("success",False))
            if "testResult" not in event:
                continue
            key = ident["testResult"]
            owner = key["label"]
            name = "linux" if owner == NATIVE else ARM+"-netstack"
            assert owner in owners and owner not in seen and name not in REPORT["cells"]
            seen.add(owner)
            result = event["testResult"]
            cell = REPORT["cells"][name] = {"name":name, "owner":owner, "config":key["configuration"]["id"],
                                            "status":result["status"], "verified":False, "files":{}}
            try:
                collect(owner, result, key, cell)
            except Exception as error:
                cell["error"] = repr(error)[:1000]
            save()
    for owner in seen:
        cell = REPORT["cells"]["linux" if owner == NATIVE else ARM+"-netstack"]
        cell["completion"] = completions.get(owner)
        if not cell["completion"] or cell["config"] != cell["completion"][0]:
            cell["verified"] = False
            cell["completion_error"] = True
    save()
    return seen



def main():
    global ARM
    assert platform.machine() == "x86_64" and os.geteuid() != 0
    previous.OUT = WORK
    previous.REPORT["assets"] = REPORT["assets"]
    h.WORK, h.OUT = WORK, OUT
    h.REPORT, h.record, h.save = REPORT, record, save
    flags = ["--test_env=GO_TEST_WRAP_TESTV=1", "--zip_undeclared_test_outputs",
             "--test_filter=^"+MAIN+"(BeforeDeadline|AfterFirstProbe)?$"]
    source = fixture()
    REPORT["injected_test_sha256"] = h.sha(source)
    for arm, rev, owners, minutes in (("fixed", FIX, [NETSTACK], 58),):
        ARM = arm
        git("checkout", "-q", "--detach", rev)
        assert git("rev-parse","HEAD").decode().strip() == rev and git("diff","HEAD","--") == b""
        Path(SOURCE).write_bytes(source)
        assert git("diff","--name-only").decode().splitlines() == [SOURCE]
        assert Path(CONNECT).read_bytes() == git("show",rev+":"+CONNECT)
        try:
            h.run(arm, owners, flags, minutes)
            completed = [row for phase,row in REPORT["commands"].items()
                         if phase.startswith(arm+"-") and not row.get("recovered")]
            assert completed and all(row["exit"] == (3 if arm == "stock" and NETSTACK in row["owners"] else 0)
                                     for row in completed), completed
        except Exception as error:
            REPORT["errors"][arm] = repr(error)[:1800]
        finally:
            git("restore", "--", SOURCE)
            save()
    REPORT["verified"] = (not REPORT["errors"] and set(REPORT["cells"]) == {"fixed-netstack"}
                          and all(c["verified"] for c in REPORT["cells"].values()))
    save()
    return not REPORT["verified"]


if __name__ == "__main__":
    try:
        status = main()
    except Exception as error:
        REPORT["errors"]["setup"] = repr(error)[:1800]
        save()
        status = 1
    sys.exit(status)
