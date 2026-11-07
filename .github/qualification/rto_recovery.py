"""Check the proposed gVisor sender against its TCP_INFO and packet capture."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import time
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
import zipfile


def need(value, reason):
    if not value:
        raise ValueError(reason)


def sacks(opts):
    blocks = []
    while opts:
        kind = opts[0]
        if kind == 0:
            break
        if kind == 1:
            opts = opts[1:]
            continue
        need(len(opts) >= 2 and 2 <= opts[1] <= len(opts), "invalid TCP option")
        size = opts[1]
        if kind == 5:
            need(size >= 10 and (size - 2) % 8 == 0, "invalid SACK option")
            blocks.extend(struct.iter_unpack("!II", opts[2:size]))
        opts = opts[size:]
    return blocks


def inspect_pcap(data):
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
                opts=tcp[20:off],
                sacks=sacks(tcp[20:off]),
                payload=tcp[off:],
                timestamp=[sec, subsec],
            )
        )
    # All seven expanding SACK blocks have the same cumulative ACK and
    # right edge. Use that controlled flow and its observed segment size.
    marks = [(i, f) for i, f in enumerate(frames) if f["sacks"]]
    need(len(marks) == 7, "expected seven SACK reports")
    first_mark, mark = marks[0]
    peer = mark["route"], mark["sport"], mark["dport"]
    sender = mark["route"][::-1], mark["dport"], mark["sport"]
    def flow(f):
        return f["route"], f["sport"], f["dport"]
    base = mark["ack"]
    def offset(seq):
        return (seq - base) & 0xffffffff
    right = offset(mark["sacks"][0][1])
    mss, rem = divmod(right, 8)
    need(mss > 0 and not rem, "flight boundary")
    blocks = []
    for _, f in marks:
        need(flow(f) == peer and f["ack"] == base and len(f["sacks"]) == 1, "SACK flow")
        blocks.append([offset(x) // mss for x in f["sacks"][0]])
        need(all(offset(x) % mss == 0 for x in f["sacks"][0]), "partial SACK")
    need(blocks == [[i, 8] for i in range(7, 0, -1)], "unexpected SACK blocks")
    heads = [i for i, f in enumerate(frames) if flow(f) == sender and f["seq"] == base and len(f["payload"]) == mss]
    need(len(heads) >= 3 and heads[0] < first_mark < heads[1], "missing original or repaired head")
    positions = [offset(f["seq"]) for f in frames[heads[0]:first_mark] if flow(f) == sender and f["payload"]]
    need(positions == list(range(0, 8*mss, mss)), "initial flight")
    need(all(f["ack"] == base for f in frames[first_mark:heads[-1]+1] if flow(f) == peer and f["flags"] & 16), "cumulative ACK advanced")
    return {"mss": mss, "original_segments": len(positions), "head_transmissions": len(heads), "sack_blocks_in_segments": blocks, "ack_advance": 0}


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "rto-interrupts-recovery"
CASE = "TestRTOInterruptsCubicRecovery"
TARGETS = ["//test/packetimpact/tests:tcp_rack_netstack_test"]
SOURCE = "8427512c2e8f6c0f1ece278d57df65202da6f0e8"
PARENT = "eef8f638667640af9d8d49aac7206ecaa2d5c8fe"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(value):
    (OUT / "result.json").write_text(json.dumps(value, indent=2) + "\n")


def record(event, row, expected=None):
    ident = event["id"]["testResult"]
    label = ident["label"]
    result = event["testResult"]
    strat = result.get("executionInfo", {})
    row.update(
        status=result["status"],
        strategy=strat.get("strategy"),
        cached=bool(strat.get("cachedRemotely") or result.get("cachedLocally")),
        run=ident.get("run", 1),
        shard=ident.get("shard", 1),
        attempt=ident.get("attempt", 1),
        outputs={},
    )
    pkg, target = label.removeprefix("//").split(":")
    root = (Path("bazel-testlogs") / pkg / target).resolve()
    for f in result.get("testActionOutput", []):
        name = f["name"]
        if name not in ("test.log", "test.xml", "test.outputs__outputs.zip"):
            continue
        if expected and name == "test.outputs__outputs.zip":
            continue
        url = urlsplit(f["uri"])
        assert url.scheme == "file" and not url.netloc and name not in row["outputs"]
        path = Path(unquote(url.path)).resolve()
        assert path.is_relative_to(root) and 0 < path.stat().st_size <= 30 << 20
        data = path.read_bytes()
        h = sha(data)
        assert not f.get("digest") or f["digest"] == h
        row["outputs"][name] = {"bytes": len(data), "sha256": h}
        if name == "test.log":
            text = data.decode(errors="replace")
            row["go_cases"] = re.findall(
                r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test[\w/]+) \(", text.replace("\x16", "")
            )
            if expected is not None:
                # Keep only numeric assertion values from the named prior
                # threshold controls; do not publish unfiltered Go test logs.
                row["threshold_assertions"] = {
                    "first":[list(map(int, m)) for m in re.findall(r"threshold after first timeout = (\d+), want (\d+)", text)],
                    "range":[list(map(int, m)) for m in re.findall(r"threshold on timeout of bytes \[(\d+),(\d+)\) = (\d+), want (\d+)", text)],
                    "repeat":[list(map(int, m)) for m in re.findall(r"threshold on third timeout, without another ACK = (\d+), want (\d+)", text)],
                }
            for key, label in (("recovery", "fast recovery"), ("loss", "timeout loss")):
                hits = re.findall(rf"{label} state=(\d+) threshold=(\d+) cwnd=(\d+)\b", text)
                if len(hits) == 1:
                    state, threshold, cwnd = map(int, hits[0])
                    row[key] = {"state": state, "threshold": threshold, "cwnd": cwnd}
            row["threshold_failure"] = "ssthresh when a timeout interrupts fast recovery =" in text
            row["diagnostics"] = [s for s in ("fast retransmission of the head:", "cannot distinguish another threshold reduction", "timeout retransmission of the head:", "operation not permitted", "permission denied") if s in text.lower()]
        elif name == "test.xml":
            xml = ElementTree.fromstring(data)
            row["xml_cases"] = len(list(xml.iter("testcase")))
            row["xml_problems"] = sum(
                1
                for c in xml.iter("testcase")
                if c.get("status") in ("skipped", "notrun")
                or c.get("result") in ("skipped", "suppressed")
                or any(x.tag in ("error", "failure", "skipped") for x in c)
            )
        else:
            with zipfile.ZipFile(path) as z:
                captures = [f for f in z.infolist() if f.filename.endswith(".pcap")]
                assert len(captures) == 1 and 24 <= captures[0].file_size < 8 << 20
                raw = z.read(captures[0])
                row["pcap_sha256"] = sha(raw)
                row["wire"] = inspect_pcap(raw)
    if expected is not None:
        results = row.get("go_cases", [])
        names = {name for _, name in results}
        allowed = {"/".join(name.split("/")[:i]) for name in expected for i in range(1, len(name.split("/")) + 1)}
        row["verified"] = (
            row["status"] == "PASSED" and row["strategy"] in ("local", "standalone")
            and not row["cached"] and (row["run"], row["shard"], row["attempt"]) == (1, 1, 1)
            and row.get("xml_cases", 0) > 0 and row.get("xml_problems") == 0
            and expected <= names <= allowed and len(names) == len(results)
            and all(status == "PASS" for status, _ in results)
        )
        return
    before, after = row.get("recovery", {}), row.get("loss", {})
    if before.get("state") != 3 or after.get("state") != 4 or before["threshold"] <= 2:
        return
    held = after["threshold"] == before["threshold"]
    row["held_threshold"] = held
    row["verified"] = (
        row["status"] == ("PASSED" if held else "FAILED")
        and row["strategy"] in ("local", "standalone")
        and not row["cached"]
        and (row["run"], row["attempt"], row["shard"]) == (1, 1, 1)
        and row.get("go_cases") == [("PASS" if held else "FAIL", CASE)]
        and row.get("xml_cases", 0) > 0
        and bool(row.get("xml_problems")) == (not held)
        and row.get("threshold_failure") == (not held)
        and bool(row.get("wire"))
    )


def check_controls(report, base_args, deadline):
    parity = "TestRTOAfterPartialACK"
    repeated = "TestRepeatedRetransmissionTimeoutThreshold"
    expected = {target:{parity + "/no_ack", parity + "/half_segment"} for target in TARGETS}
    go_owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
    expected[go_owner] = {repeated + "/" + cc + "/" + case for cc in ("reno", "cubic")
                          for case in ("same_segment", "sack_only", "partial_ack", "next_segment")}
    phase = report["controls"] = {"owners":{}, "finish":[], "verified":False}
    save(report)
    bep = OUT / "control-events"
    cmd = [*base_args, "--test_filter=^(" + parity + "|" + repeated + ")$",
           "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false", *expected]
    log = OUT / "controls.log"
    try:
        with log.open("wb") as f:
            phase["bazel_exit"] = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                                 timeout=max(1, deadline-time.monotonic())).returncode
    except subprocess.TimeoutExpired:
        phase["bazel_exit"] = 124
    if bep.is_file():
        for line in bep.open():
            if not line.endswith("\n"):
                continue
            ev = json.loads(line)
            if "started" in ev:
                phase["invocation"] = ev["started"].get("uuid")
            if "finished" in ev:
                phase["finish"].append(ev["finished"].get("exitCode", {}).get("code", 0))
            if "testResult" not in ev:
                continue
            label = ev["id"]["testResult"]["label"]
            assert label in expected and label not in phase["owners"]
            row = phase["owners"][label] = {"verified":False}
            try:
                record(ev, row, expected[label])
            except (AssertionError, OSError, ValueError, KeyError, ElementTree.ParseError) as exc:
                row["error"] = type(exc).__name__
            save(report)
    phase["verified"] = (phase["bazel_exit"] == 0 and phase["finish"] == [0] and
                         set(phase["owners"]) == set(expected) and all(r["verified"] for r in phase["owners"].values()))


def main():
    deadline = time.monotonic() + 2950
    report = {
        "source": git("rev-parse", "HEAD"),
        "parent": git("rev-parse", "HEAD^"),
        "machine": platform.machine(),
        "public_head": "4a4e33cfc5dede7d3bfbfc8b43b7784578178eb6",
        "paired_comparison": {"run": 42378653816, "source": "8a79ae452452c827dc6884dd032aec947f351539"},
        "previous_correction": {"run":42376142305, "artifact":13576347913, "sha256":"3c2d23a52329f516b3be82361e7f3db0af160af62398fb094c17c38f539c0b46"},
        "baseline": {"run":42373671016, "artifact":13574977842, "sha256":"7ab6309c14aeec29422b063f989669bb451eae86c4d8d395ee5a494d1e28ddc0"},
        "owners": {},
        "verified": False,
    }
    save(report)
    assert (
        report["source"] == SOURCE
        and report["parent"] == PARENT
        and report["machine"] == "x86_64"
    )
    bep = OUT / "events"
    cmd = [
        str(TMP / "bazel"),
        "--nosystem_rc",
        "--nohome_rc",
        "--host_jvm_args=-Xmx4g",
        "test",
        "--config=x86_64",
        "--jobs=3",
        "--strategy=TestRunner=local",
        "--test_env=GO_TEST_WRAP_TESTV=1",
        "--test_sharding_strategy=disabled",
        "--nocache_test_results",
        "--flaky_test_attempts=1",
        "--runs_per_test=1",
        "--test_timeout=200",
        "--keep_going",
        "--remote_cache=",
        "--remote_executor=",
        "--bes_backend=",
        "--zip_undeclared_test_outputs",
        "--test_filter=^" + CASE + "$",
        "--build_event_json_file=" + str(bep),
        "--build_event_json_file_path_conversion=false",
        *TARGETS,
    ]
    with (OUT / "bazel.log").open("wb") as log:
        report["bazel_exit"] = subprocess.run(
            cmd, stdout=log, stderr=subprocess.STDOUT, timeout=2850
        ).returncode
    console = (OUT / "bazel.log").read_text(errors="replace").lower()
    report["diagnostics"] = [
        s
        for s in (
            "no such target",
            "no such package",
            "no space left",
            "not a recognized option",
            "error downloading",
            "compilation of rule",
            "out of memory",
        )
        if s in console
    ]
    ends = []
    if bep.is_file():
        for line in bep.open():
            e = json.loads(line)
            if "started" in e:
                report["invocation"] = e["started"].get("uuid")
            if "finished" in e:
                ends.append(e["finished"].get("exitCode", {}).get("code", 0))
            if "testResult" in e:
                label = e["id"]["testResult"]["label"]
                assert label in TARGETS and label not in report["owners"]
                report["owners"][label] = row = {"verified": False}
                try:
                    record(e, row)
                except (
                    AssertionError,
                    OSError,
                    ValueError,
                    KeyError,
                    StopIteration,
                    struct.error,
                    zipfile.BadZipFile,
                    ElementTree.ParseError,
                ) as e:
                    row["error"] = type(e).__name__
                save(report)
    report["finish"] = ends
    report["wire_passed"] = (
        ends == [0]
        and report["bazel_exit"] == 0
        and set(report["owners"]) == set(TARGETS)
        and all(r["verified"] and r.get("held_threshold") is True for r in report["owners"].values())
    )
    save(report)
    base_args = cmd[:cmd.index("--test_filter=^" + CASE + "$")]
    check_controls(report, base_args, deadline)
    report["verified"] = report["wire_passed"] and report["controls"]["verified"]
    save(report)
    return report["verified"]


if __name__ == "__main__":
    raise SystemExit(not main())
