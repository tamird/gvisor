"""Replay seven isolated application transfers on each of three master-based senders."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

BASE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
PUBLISHED = "4a4e33cfc5dede7d3bfbfc8b43b7784578178eb6"
AMENDED = "8427512c2e8f6c0f1ece278d57df65202da6f0e8"
RETRY = "4867d503b71f6c13f0346778ca61373df04ed0ae"
TCP = Path("pkg/tcpip/transport/tcp")
EXPECTED = {
    "cubic.go": "39a44db54f2b46436f867683db5ac420232c1f69",
    "reno.go": "f89d40ef64b912ad65453aa8ad753eb84bec5dcf",
    "published": "c3ee0e43a06c4d70076e97c10021a59179a9ae36",
    "amended": "b44e4841070a5a8177428a00991b65dda6b8e45d",
    "combined": "5430ce6c7d600ba2c141e1b743b46ab6ef6e31e6",
}
LABEL = "//test/tcp_recovery_replay:heavy_loss"
TRIALS = [("none", 2)] + [("burst", i) for i in (2, 3, 5, 9, 12, 15)]
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "rto-diagnostics"
OUT.mkdir(exist_ok=True)
REPORT = {"base": BASE, "published": PUBLISHED, "amended": AMENDED, "retry": RETRY,
          "reporter": "f72f18f2f7fe0e5ea4151b51ce3f67d68b3b05e2", "arms": {}}


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def save():
    (OUT / "receipt.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def integrate(path, revision):
    base = OUT / "common-source"
    base.write_bytes(subprocess.check_output(["git", "show", f"{revision}^:{path}"]))
    candidate = OUT / "proposed-source"
    candidate.write_bytes(subprocess.check_output(["git", "show", f"{revision}:{path}"]))
    merged = subprocess.check_output(["git", "merge-file", "-p", path, base, candidate])
    path.write_bytes(merged)


def sources(arm):
    blobs = {}
    for name in ("cubic.go", "reno.go", "snd.go"):
        path = TCP / name
        git("restore", "--source=" + BASE, "--worktree", "--", str(path))
        integrate(path, PUBLISHED if arm == "published" else AMENDED)
        if arm == "combined" and name == "snd.go":
            integrate(path, RETRY)
        blobs[name] = git("hash-object", str(path))
        assert blobs[name] == EXPECTED[arm if name == "snd.go" else name]

    sender = TCP / "snd.go"
    original = sender.read_text()
    start = original.index("func (s *sender) retransmitTimerExpired()")
    end = original.index("\n}\n", start)
    method = original[start:end]
    anchor = "\tuto := s.ep.userTimeout"
    assert method.count(anchor) == 1
    method = method.replace(anchor, """	replayThreshold := s.Ssthresh
	replayActive, replayRepeat := 0, 0
	if s.FastRecovery.Active {
		replayActive = 1
	}
	if s.rtoRepeat {
		replayRepeat = 1
	}
""" + anchor)
    # All events are local to this one test child. Only the bounded numeric
    # fields for its measured transfer and port pair enter the receipt.
    def event(fields, operands):
        keys = ('local', 'remote', 'sequence', 'una', *fields)
        fmt = "{" + ",".join('"' + k + '":%d' for k in keys) + "}"
        args = ["s.ep.TransportEndpointInfo.ID.LocalPort", "s.ep.TransportEndpointInfo.ID.RemotePort",
                "uint32(s.writeList.Front().sequenceNumber)", "uint32(s.SndUna)", *operands]
        return 'fmt.Printf("TCP_RTO_EVENT "+' + chr(96) + fmt + chr(96) + '+"\\n", ' + ', '.join(args) + ')'
    m = re.findall(r"(?m)^\ts\.cc\.HandleRTOExpired\([^\n]+\)$", method)
    assert len(m) == 1
    method = method.replace(m[0], m[0] + "\n\t" + event(
        ("phase", "active", "repeat", "before", "after", "elapsed_ms"),
        ("0", "replayActive", "replayRepeat", "replayThreshold", "s.Ssthresh", "elapsed.Milliseconds()")))
    marker = "\t\ts.ep.stack.Stats().TCP.EstablishedTimedout.Increment()"
    phases = [3] if arm == "combined" else [2, 1]
    parts = method.split(marker)
    assert len(parts) == 1 + len(phases)
    method = parts[0]
    for phase, part in zip(phases, parts[1:], strict=True):
        close = event(("phase", "xmit", "limit", "elapsed_ms", "uto_ms", "probes", "active", "repeat", "before", "at_close"),
                      (str(phase), "s.writeList.Front().xmitCount", "s.maxRetries",
                       "elapsed.Milliseconds()", "uto.Milliseconds()", "s.unackZeroWindowProbes", "replayActive", "replayRepeat", "replayThreshold", "s.Ssthresh"))
        method += "\t\t" + close + "\n" + marker + part
    sender.write_text(original[:start] + method + original[end:])
    return blobs


def snapshot(state, initial):
    if state is None:
        return None
    keys = ("state", "cwnd", "wnd", "outstanding", "sacked", "recovery", "rto_ms",
            "send_buffer_size", "send_buffer_used", "recv_buffer_used", "recv_memory_used",
            "recv_window", "pending_bytes", "pending_segments", "resend_armed", "reorder_armed",
            "probe_armed", "zero_window_probing", "unanswered_zero_window_probes")
    result = {key: state[key] for key in keys}
    for key in ("una", "nxt", "write_front", "write_next", "write_last", "recv_nxt", "first_pending"):
        origin = initial["recv_nxt" if key in ("recv_nxt", "first_pending") else "nxt"]
        v = state[key]
        result[key + "_offset"] = None if v < 0 else ((v - origin + 2**31) % 2**32 - 2**31)
    return result


def sample(s):
    row = {key: s[key] for key in ("seed", "loss", "operation", "requested", "client_read",
           "client_timed_out", "ms", "writer_requests", "server_requested", "server_attempted",
           "server_written", "server_errors", "sender_rto_delta", "sender_retransmits_delta",
           "sent_packets", "delivered_packets", "dropped_packets", "queue_drops", "zero_window_acks",
           "missing_wire_bytes", "never_sent_bytes", "sent_undelivered_bytes", "capture_limit")}
    row["observed_rtos"] = len(s["observed_rtos_ms"] or [])
    before = s["server_before"]
    peer = s["peer_before"]
    row["controller"] = {key:before.get(key, 0) for key in ("cubic_controller", "negotiated_sack", "negotiated_ts", "rack_enabled")}
    def pair(endpoint):
        return endpoint["local_port"], endpoint["remote_port"]
    endpoints = pair(before) == pair(peer)[::-1] and min(pair(before)) > 0
    for role, initial in (("server", before), ("peer", peer)):
        after = s[role + "_after"]
        endpoints = endpoints and (not after or pair(after) == pair(initial))
        row[role + "_before"] = snapshot(initial, initial)
        row[role + "_after"] = snapshot(after, initial)
    gaps = s["first_wire_gaps"] or []
    row["first_gap"] = {k: gaps[0][k] for k in ("start", "end", "sent", "script_drops", "transmitted")} if gaps else None
    row["close_witnesses"] = []
    row["active_controller_timeouts"] = []
    events_matched = True
    data_timeouts = 0
    for w in s["rto_witnesses"]:
        side = (w["local"], w["remote"])
        role = "server" if side == pair(before) else "peer" if side == pair(peer) else "unknown"
        events_matched &= role != "unknown"
        if w["phase"] in (1, 2, 3):
            row["close_witnesses"].append(dict(role=role, branch={1:"per_segment_limit", 2:"user_timeout_or_probe_limit", 3:"elapsed_budget_or_probe_limit"}[w["phase"]], xmit=w["xmit"], limit=w["limit"],
                                               elapsed_ms=w["elapsed_ms"], user_timeout_ms=w["uto_ms"],
                                               probes=w["probes"], active_at_entry=w["active"], repeat_at_entry=w["repeat"], threshold_at_entry=w["before"], threshold_at_close=w["at_close"], at_first_gap=bool(gaps) and role == "server" and w["sequence_offset"] == gaps[0]["start"]))
        elif w["phase"] == 0:
            if role == "server":
                data_timeouts += 1
                if w["active"]:
                    row["active_controller_timeouts"].append({key:w[key] for key in ("before","after","repeat","una_offset","sequence_offset","elapsed_ms")})
        else:
            raise ValueError("unknown RTO event")
    row["server_controller_timeouts"] = data_timeouts
    row["payload_verified_bytes"] = s["client_read"]
    deadline = bool(s["client_error"])
    request = s["requested"]
    valid = (endpoints and s["operation"] == "response_read" and request == 2_800_000 and s["writer_requests"] == 3
             and s["server_requested"] == request and not s["capture_limit"] and s["sent_packets"] > 0
             and s["client_timed_out"] == deadline and 0 <= s["client_read"] <= request
             and s["missing_wire_bytes"] == s["never_sent_bytes"] + s["sent_undelivered_bytes"]
             and (s["loss"] == "burst") == (s["dropped_packets"] > 0)
             and events_matched and all(row["controller"][k] == 1 for k in ("cubic_controller","negotiated_sack","rack_enabled")))
    if deadline:
        valid = valid and s["loss"] == "burst" and s["ms"] >= 29_900 and s["client_read"] < request
    else:
        valid = valid and s["client_read"] == request and s["missing_wire_bytes"] == 0 and not gaps and bool(s["server_after"]) and bool(s["peer_after"])
    row.update(endpoints_matched=endpoints, valid=valid)
    return row


def run(arm):
    blobs = sources(arm)
    row = REPORT["arms"][arm] = {"runtime_blobs": blobs, "observed_sender_blob": git("hash-object", str(TCP / "snd.go")), "samples": []}
    save()
    bep = TMP / ("rto-" + arm + ".bep")
    cmd = [str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g",
           "test", "--config=x86_64", "--jobs=3", "--test_output=errors", "--color=no",
           "--nocache_test_results", "--strategy=TestRunner=local", "--test_timeout=860",
           "--test_env=TCP_REPLAY_OUTER_NETNS=" + os.readlink("/proc/self/ns/net"),
           "--test_arg=-test.v", "--test_env=TCP_REPLAY_ARM=" + arm, "--build_event_json_file=" + str(bep),
           "--run_under=/usr/bin/unshare --user --map-root-user --net --", LABEL]
    with (TMP / (arm + ".bazel.log")).open("w") as out:
        row["exit"] = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
    save()
    if not bep.exists():
        return
    events = []
    for line in bep.open():
        event = json.loads(line)
        if event.get("id", {}).get("testResult", {}).get("label") == LABEL and "testResult" in event:
            events.append(event["testResult"])
        if "finished" in event:
            row["bazel_exit_name"] = event["finished"].get("exitCode", {}).get("name")
    if row["exit"]:
        private = (TMP / (arm + ".bazel.log")).read_text(errors="replace")
        row["failure_locations"] = sorted(set(re.findall(r"(?:replay_test|observe|snd|rack)\.go:\d+", private)))[:16]
        row["failure_kinds"] = [k for k in ("undefined:", "no such package", "no matching toolchains",
                                            "resource exhausted", "cannot load", "compilation failed",
                                            "context deadline exceeded", "exec format error") if k in private.lower()]
    row["test_statuses"] = [e["status"] for e in events]
    save()
    log = Path("bazel-testlogs/test/tcp_recovery_replay/heavy_loss/test.log")
    if len(events) != 1 or not log.is_file():
        return
    data = log.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    row["test_log_sha256"] = sha
    refs = [f for f in events[0].get("testActionOutput", []) if f.get("name") == "test.log"]
    for f in refs:
        uri = urlsplit(f.get("uri", ""))
        if f.get("digest") == sha or (not f.get("digest") and uri.scheme == "file" and not uri.netloc and Path(unquote(uri.path)).resolve() == log.resolve()):
            break
    else:
        save()
        return
    info = events[0].get("executionInfo", {})
    row["strategy"] = info.get("strategy")
    row["cached"] = bool(info.get("cachedRemotely") or events[0].get("cachedLocally"))
    xml = log.with_name("test.xml")
    if not xml.is_file():
        return
    xml_blob = xml.read_bytes()
    row["xml_sha256"] = hashlib.sha256(xml_blob).hexdigest()
    xml_refs = [v for v in events[0].get("testActionOutput", []) if v.get("name") == "test.xml"]
    if not any(v.get("digest") == row["xml_sha256"] or (not v.get("digest") and urlsplit(v.get("uri","")).scheme == "file" and Path(unquote(urlsplit(v["uri"]).path)).resolve() == xml.resolve()) for v in xml_refs):
        return
    cases = list(ET.fromstring(xml_blob).iter("testcase"))
    row["xml_cases"] = len(cases)
    row["xml_problems"] = sum(any(c.find(tag) is not None for tag in ("failure","error","skipped")) or c.get("result") in ("skipped","suppressed") or c.get("status") in ("notrun","skipped") for c in cases)
    counts = []
    for line in data.decode(errors="replace").splitlines():
        line = line.removeprefix("\x16")
        if line.startswith("TCP_SAMPLE arm=" + arm + " "):
            raw = json.loads(line.split(" ", 2)[2])
            try:
                obs = sample(raw)
            except (KeyError, IndexError, TypeError, ValueError) as ex:
                obs = {"seed": raw.get("seed"), "loss": raw.get("loss"), "valid": False,
                       "projectionError": type(ex).__name__, "client_timed_out": raw.get("client_timed_out")}
            row["samples"].append(obs)
            save()
        if re.search(r"--- FAIL: TestMeasure/", line):
            row.setdefault("failed_subtests", []).extend(re.findall(r"TestMeasure/(?:burst|none)_\d+", line))
        if m := re.fullmatch(r"TCP_COUNTS arm=" + arm + r" complete=(\d+) deadline=(\d+)", line):
            counts.append(tuple(map(int, m.groups())))
    rows = row["samples"]
    deadlines = sum(r["client_timed_out"] is True for r in rows)
    row["complete"], row["deadlines"] = len(rows) - deadlines, deadlines
    row["valid"] = (row["exit"] == 0 and row["test_statuses"] == ["PASSED"]
                    and row["strategy"] in ("local","standalone") and not row["cached"] and row["xml_cases"] > 0 and row["xml_problems"] == 0
                    and row["bazel_exit_name"] == "SUCCESS" and [(r["loss"], r["seed"]) for r in rows] == TRIALS and all(r["valid"] for r in rows)
                    and counts == [(row["complete"], deadlines)])
    save()
    print(arm, "complete", row["complete"], "deadlines", deadlines, "valid", row["valid"], flush=True)


assert git("rev-parse", "HEAD^") == BASE
assert git("rev-parse", PUBLISHED + "^") == git("rev-parse", AMENDED + "^") == "eef8f638667640af9d8d49aac7206ecaa2d5c8fe"
assert git("rev-parse", RETRY + "^") == "f47e3a56425482ba7090735302d6454757086d51"
for arm in ("published", "amended", "combined"):
    try:
        run(arm)
    except (KeyError, ValueError, IndexError, AssertionError, OSError, subprocess.CalledProcessError) as ex:
        REPORT["arms"].setdefault(arm, {})["measurement_error"] = type(ex).__name__
        save()
assert all(REPORT["arms"].get(arm, {}).get("valid") for arm in ("published", "amended", "combined"))
