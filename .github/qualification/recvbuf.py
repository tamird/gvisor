"""Join each POSIX read to the offered segment and cumulative ACK in the same packet capture."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import signal
import struct
import subprocess
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

def need(value, reason):
    if not value:
        raise ValueError(reason)


def parse_pcap(data):
    need(len(data) >= 24 and len(data) < 8 << 20, "pcap size")
    endians = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\x3c\x4d": ">",
    }
    endian = endians[data[:4]]
    need(struct.unpack_from(endian + "I", data, 20)[0] == 1, "expected Ethernet pcap")
    frames = []
    pos = 24
    while pos < len(data):
        need(pos + 16 <= len(data), "truncated record")
        sec, subsec, size, original = struct.unpack_from(endian + "IIII", data, pos)
        pos += 16
        need(size <= original and pos + size <= len(data), "truncated frame")
        fr = data[pos : pos + size]
        pos += size
        if len(fr) < 34 or fr[12:14] != b"\x08\x00" or fr[23] != 6:
            continue
        iplen = struct.unpack_from("!H", fr, 16)[0]
        iph = (fr[14] & 15) * 4
        start = 14 + iph
        need(
            iph >= 20 and len(fr) >= 14 + iplen and iplen >= iph + 20,
            "short IPv4 TCP frame",
        )
        tcp = fr[start : 14 + iplen]
        off = (tcp[12] >> 4) * 4
        need(20 <= off <= len(tcp), "invalid TCP data offset")
        sport, dport, seq, ack = struct.unpack_from("!HHII", tcp)
        frames.append(
            dict(
                route=(fr[26:30], fr[30:34]),
                sport=sport,
                dport=dport,
                seq=seq,
                ack=ack,
                flags=tcp[13],
                window=struct.unpack_from('!H', tcp, 14)[0],
                payload=tcp[off:], options=tcp[20:off],
                ns=sec*1_000_000_000 + subsec*(1 if data[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1000),
            )
        )
    return frames


OUT = Path(os.environ["RUNNER_TEMP"]) / "recvbuf"
TARGETS = ["//test/packetimpact/tests:tcp_rcv_buf_space_" + dut + "_test" for dut in ("native", "netstack")]
MASK = (1 << 32) - 1


def save(report):
    tmp = OUT / "result.tmp"
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    tmp.replace(OUT / "result.json")


def check_packets(frames, log):
    observations = {}
    pattern = r"DRAIN case=(\w+) port=(\d+) ack=(\d+) mss=(\d+) tcpinfo_rcv_mss=(\d+) send_ns=(\d+) peek_ns=(\d+) read_start_ns=(\d+) read_end_ns=(\d+)"
    for name, port, ack, mss, rcv_mss, sent, peeked, start, end in re.findall(pattern, log):
        port, ack, mss, rcv_mss, sent, peeked, start, end = map(int, (port, ack, mss, rcv_mss, sent, peeked, start, end))
        need(name not in observations, "duplicate control")
        syn, = [p for p in frames if p["flags"] == 2 and p["dport"] == port]
        offered = [p for p in frames if p["route"] == syn["route"] and p["sport"] == syn["sport"] and p["dport"] == port and p["payload"]]
        need(len(offered) == 25 and all(len(p["payload"]) == mss for p in offered), "offered train")
        need(all(p["seq"] == (syn["seq"] + 1 + i * mss) & MASK for i, p in enumerate(offered)), "sequence")
        last = offered[-1]
        need(bool(last["flags"] & 8) == (name != "no_push_drain"), "PSH control")
        need(ack == (last["seq"] + mss) & MASK, "read marker sequence")
        reverse = syn["route"][::-1]
        synack, = [p for p in frames if p["route"] == reverse and p["sport"] == port and
                   p["dport"] == syn["sport"] and p["flags"] == 0x12]
        opts = synack["options"]
        advertised = []
        while opts and opts[0] != 0:
            if opts[0] == 1:
                opts = opts[1:]
                continue
            need(len(opts) >= 2 and 2 <= opts[1] <= len(opts), "SYN option length")
            if opts[0] == 2:
                need(opts[1] == 4, "MSS option length")
                advertised.append(int.from_bytes(opts[2:4], "big"))
            opts = opts[opts[1]:]
        need(advertised == [mss], "test payload differs from advertised MSS")
        pure_acks = [p for p in frames if p["route"] == syn["route"][::-1] and
                     p["sport"] == port and p["dport"] == syn["sport"] and p["flags"] == 0x10 and not p["payload"]]
        for segment, following in zip(offered, offered[1:]):
            milestone = (segment["seq"] + len(segment["payload"])) & MASK
            need(any(p["ack"] == milestone and segment["ns"] <= p["ns"] <= following["ns"] for p in pure_acks),
                 "warm ACK missing before next segment")
        replies = [p for p in pure_acks if p["ack"] == ack and p["ns"] >= last["ns"]]
        need(replies and end >= start and start >= last["ns"], "missing or unordered observation")
        need(sent - 1_000_000 <= last["ns"] <= peeked + 1_000_000 and peeked <= start, "capture clock disagrees with socket calls")
        arrived = replies[0]["ns"]
        observations[name] = {"port": port, "ack": ack, "mss": mss, "tcpinfo_rcv_mss": rcv_mss, "offered_segments": len(offered),
                              "peek_end_us": (peeked-last["ns"])//1000, "read_start_us": (start-last["ns"])//1000, "read_end_us": (end-last["ns"])//1000,
                              "ack_us": (arrived-last["ns"])//1000, "ack_from_read_start_us": (arrived-start)//1000}
    need(set(observations) == {"push_drain", "push_leave_one", "no_push_drain"}, "missing control")
    return observations


def collect(row, event):
    result = event["testResult"]
    row.update(status=result["status"], strategy=result.get("executionInfo",{}).get("strategy"),
               cached=bool(result.get("cachedLocally") or result.get("executionInfo",{}).get("cachedRemotely")),files={})
    ident = event["id"]["testResult"]
    owner = ident["label"]
    assert owner in TARGETS and 1 <= ident["run"] <= 4
    stem = ("native" if owner == TARGETS[0] else "netstack") + "-" + str(ident["run"])
    docs = {}
    for record in result["testActionOutput"]:
        name = record["name"]
        if name not in ("test.log", "test.xml", "test.outputs__outputs.zip"):
            continue
        url = urlsplit(record["uri"])
        assert url.scheme == "file" and not url.netloc
        data = Path(unquote(url.path)).read_bytes()
        h = hashlib.sha256(data).hexdigest()
        assert len(data) <= 32 << 20 and (not record.get("digest") or record["digest"] == h)
        if "length" in record:
            assert int(record["length"]) == len(data)
        row["files"][name] = [h, len(data)]
        docs[name] = data
        if name in ("test.log", "test.xml"):
            (OUT / (stem + "." + name.rsplit(".",1)[1])).write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(docs["test.outputs__outputs.zip"])) as archive:
        name, = [n for n in archive.namelist() if n.endswith(".pcap")]
        assert 0 < archive.getinfo(name).file_size <= 8 << 20
        raw = archive.read(name)
    (OUT / (stem + ".pcap")).write_bytes(raw)
    row["pcap_sha256"] = hashlib.sha256(raw).hexdigest()
    log = docs["test.log"].decode(errors="replace")
    row["go_cases"] = re.findall(r"--- (PASS|FAIL|SKIP): ([^ \r\n]+) [(]", log)
    row["go_diagnostics"] = [s.strip()[:440] for s in log.splitlines() if "tcp_rcv_buf_space_test.go:" in s][:20]
    root = ET.fromstring(docs["test.xml"])
    row["xml_cases"] = [c.get("name") for c in root.iter("testcase")]
    row["drain_controls"] = check_packets(parse_pcap(raw), log)
    assert result["status"] == "PASSED" and not row["cached"] and row["strategy"] in ("local","standalone")
    assert row["go_cases"] == [("PASS","TestACKOnDrain")] and row["xml_cases"] == ["TestACKOnDrain"]
    assert not any(c.tag in {"error","failure","skipped"} for c in root.iter())


def read_events(bep, offset, report, elapsed):
    if not bep.exists():
        return offset
    scope = report["ack_drain"]
    with bep.open("rb") as stream:
        stream.seek(offset)
        for line in stream:
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            event = json.loads(line)
            if "started" in event:
                scope["invocation"] = event["started"]["uuid"]
            elif "completed" in event and event["id"]["targetCompleted"]["label"] in TARGETS:
                owner = event["id"]["targetCompleted"]["label"]
                scope["target_completions"][owner] = [elapsed, event["completed"].get("success", False)]
            elif "testResult" in event:
                ident = event["id"]["testResult"]
                owner = ident["label"]
                key = owner + "/" + str(ident["run"])
                assert owner in TARGETS and key not in scope["test_results"]
                row = scope["test_results"][key] = {"ident": ident, "received_seconds": elapsed}
                result = event["testResult"]
                row["attempt_start"] = result.get("testAttemptStart", result.get("testAttemptStartMillisEpoch"))
                row["duration"] = result.get("testAttemptDuration", result.get("testAttemptDurationMillis"))
                try:
                    collect(row,event)
                    row["verified"] = True
                except Exception as exc:
                    row["error"] = type(exc).__name__ + " " + str(exc)[:900]
            else:
                continue
            save(report)
    return offset


def main():
    assert platform.machine() == "x86_64" and not (OUT / "auth").exists()
    scope = {"test_results": {}, "target_completions": {}}
    report = {"verified": False, "pr_head": "981034f8b53c9f741b26d1c640e5ac8d4fcdc1d8",
              "source": subprocess.check_output(["git", "rev-parse", "HEAD"],text=True).strip(),
              "kernel": platform.release(), "ack_drain": scope}
    save(report)
    bep = OUT / "controlled.jsonl"
    log_path = OUT / "controlled.log"
    cmd = [str(OUT.parent / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
           "test", "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local",
           "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results", "--test_sharding_strategy=disabled",
           "--runs_per_test=4", "--flaky_test_attempts=1", "--test_timeout=360", "--keep_going",
           "--distdir=" + str(OUT / "dist"), "--remote_executor=", "--remote_cache=", "--bes_backend=",
           "--zip_undeclared_test_outputs", "--test_filter=^TestACKOnDrain$",
           "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false", *TARGETS]
    offset = 0
    start = time.monotonic()
    with log_path.open("w") as log, subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT) as proc:
        while proc.poll() is None:
            elapsed = round(time.monotonic() - start, 2)
            offset = read_events(bep, offset, report, elapsed)
            if elapsed >= 50 * 60:
                scope["deadline_seconds"] = 50 * 60
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        scope["exit"] = proc.wait()
    scope["seconds"] = round(time.monotonic() - start, 2)
    read_events(bep, offset, report, scope["seconds"])
    # The fresh native process has no credentials. Keep only bounded diagnostics
    # from its public targets and any repository still needed after analysis.
    lines = log_path.read_text(errors="replace").splitlines()
    scope["errors"] = [s[:500] for s in lines if s.startswith("ERROR:")][:12]
    scope["missing_public_urls"] = sorted({u for s in lines if "Error downloading" in s
        for u in re.findall(r"https://(?:github[.]com|mirror[.]bazel[.]build|releases[.]bazel[.]build)/[A-Za-z0-9.%_~+/=-]+", s)})[:20]
    scope["last_progress"] = [s[:320] for s in lines if re.match(r"^\[[0-9,]+ / [0-9,]+\]|^INFO: (?:Analyzed|Elapsed time|[0-9,]+ processes)", s)][-6:]
    report["verified"] = (scope["exit"] == 0 and set(scope["test_results"]) == {owner + "/" + str(run) for owner in TARGETS for run in range(1,5)}
                          and all(scope["target_completions"].get(owner,[None,False])[1] for owner in TARGETS)
                          and all(row.get("verified") for row in scope["test_results"].values()))
    save(report)
    return not report["verified"]


if __name__ == "__main__":
    raise SystemExit(main())
