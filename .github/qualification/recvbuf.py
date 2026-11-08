"""Verify each receive-buffer test against its own window updates and queued TCP reply."""
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
                payload=tcp[off:],
                timestamp=[sec, subsec],
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


def check_packets(frames, row):
    syn, = [p for p in frames if p["flags"] == 2]
    route = (*syn["route"], syn["sport"], syn["dport"])
    reverse = (route[1], route[0], route[3], route[2])
    outbound = []
    inbound = []
    for index, frame in enumerate(frames):
        identity = (*frame["route"], frame["sport"], frame["dport"])
        if identity == route:
            outbound.append((index, frame))
        elif identity == reverse:
            inbound.append((index, frame))
    synack, = [p for _, p in inbound if p["flags"] == 18]
    base = (syn["seq"] + 1) & MASK
    reply_seq = (synack["seq"] + 1) & MASK
    segments = [(i, (p["seq"] - base) & MASK, len(p["payload"])) for i, p in outbound if p["payload"]]
    offered = 0
    for _, offset, length in segments:
        need(offset == offered and 0 < length <= 1400, "offered segments are not contiguous")
        offered += length
    need(offered == 8193, ("fixture SO_RCVBUF + 1", offered))
    early = [p for i, p in outbound if i <= segments[-1][0]]
    need(all(p["window"] == 0 for p in early), "nonzero window before the entire offer")
    updates = [(i, (p["seq"] - base) & MASK) for i, p in outbound if p["window"] and p["flags"] == 16]
    replies = []
    probes = []
    for i, p in inbound:
        payload = p["payload"]
        if not payload or not p["flags"] & 16:
            continue
        offset = (p["seq"] - reply_seq) & MASK
        if offset + len(payload) <= 2 and payload == b"ok"[offset:offset+len(payload)]:
            if offset + len(payload) == 2:
                replies.append((i, (p["ack"] - base) & MASK))
            else:
                probes.append(i)
    result = dict(segments=segments, offered=offered, window_updates=updates,
                  first_byte_segments=probes, second_byte_replies=replies)
    row["packets"] = result
    need(updates and replies and segments[-1][0] < updates[0][0] < replies[0][0], result)
    for index, offset in updates:
        if index >= replies[0][0]:
            break
        need(any(i < index and p["flags"] & 16 and (p["ack"] - base) & MASK == offset for i,p in inbound),
             ("window opening is not at a captured ACK", index, offset))
    result["marker_acknowledged_bytes"] = replies[0][1]
    need(0 < replies[0][1] < offered, result)
    return result


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
    check_packets(parse_pcap(raw), row)
    assert result["status"] == "PASSED" and not row["cached"] and row["strategy"] in ("local","standalone")
    assert row["go_cases"] == [("PASS","TestReduceRecvBuf")] and row["xml_cases"] == ["TestReduceRecvBuf"]
    assert not any(c.tag in {"error","failure","skipped"} for c in root.iter())


def read_events(bep, offset, report, elapsed):
    if not bep.exists():
        return offset
    scope = report["window_barrier"]
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
    report = {"verified": False, "source": "f0e4f9b4e0f820684f8a7a5b9ef145b658138efa",
              "base": "451b92800b9cb82262dc8a735388f7cf4d17d895",
              "tested_tree": "3a6b8bd62f0eebe1be782bc53d8fdd292d355663",
              "kernel": platform.release(), "window_barrier": scope,
              "older_stock_receipt": [42466764407,13613271270],
              "older_held_vs_drained_receipt": [42474704019,13615863442]}
    save(report)
    bep = OUT / "controlled.jsonl"
    log_path = OUT / "controlled.log"
    cmd = [str(OUT.parent / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
           "test", "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local",
           "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results", "--test_sharding_strategy=disabled",
           "--runs_per_test=4", "--flaky_test_attempts=1", "--test_timeout=360", "--keep_going",
           "--distdir=" + str(OUT / "dist"), "--remote_executor=", "--remote_cache=", "--bes_backend=",
           "--zip_undeclared_test_outputs", "--test_filter=^TestReduceRecvBuf$",
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
