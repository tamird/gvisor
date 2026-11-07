"""Measure the same seven isolated application transfers on two sender revisions."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

BASE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
PR = "1b9768b199524246fe8e4fa66a0b623069778780"
CONTROL = "e1e5a93215f507fe735b868493a14868180f3bec"
HARNESS = "294799abc87f7f16527ee060c1c159ed9b70a1b2"
FILES = ["pkg/tcpip/transport/tcp/rack.go", "pkg/tcpip/transport/tcp/snd.go"]
LABEL = "//test/tcp_recovery_replay:heavy_loss"
TRIALS = [("none", 2)] + [("burst", i) for i in (2, 3, 5, 9, 12, 15)]
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "rto-diagnostics"
OUT.mkdir(exist_ok=True)
REPORT = {"base": BASE, "pr": PR, "diagnostic_control": CONTROL, "harness": HARNESS,
          "reporter": "f72f18f2f7fe0e5ea4151b51ce3f67d68b3b05e2",
          "original_run": "kekawia/net-switch/42222590246", "arms": {}}


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def save():
    (OUT / "receipt.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def sources(arm):
    git("restore", "--source=" + PR, "--worktree", "--", *FILES)
    if arm == "timeout_counting":
        patch = subprocess.check_output(["git", "diff", BASE, CONTROL, "--", FILES[1]])
        subprocess.run(["git", "apply", "-"], input=patch, check=True)
    blobs = [git("hash-object", file) for file in FILES]
    assert blobs == ["352345517f6cfc3f8eb77486494bf948920202b7",
                     "819a9e07b4bdbc7be7b1a3c03eef332c3215fac4" if arm == "pr" else
                     "789e190848810267a39eb29d723bb198e4a253dd"]
    sender = Path(FILES[1])
    original = sender.read_text()
    counter = "seg.xmitCount" if arm == "pr" else "s.rtoRetries"
    anchor = f"if seg != nil && {counter} > s.maxRetries {{\n\t\ts.ep.stack.Stats().TCP.EstablishedTimedout.Increment()"
    assert original.count(anchor) == 1
    witness = r'''fmt.Printf("TCP_RETRY_LIMIT "+`{"local":%d,"remote":%d,"sequence":%d,"una":%d,"xmit":%d,"count":%d,"limit":%d,"timeouts":%d,"established_timedout":%d}`+"\n", s.ep.TransportEndpointInfo.ID.LocalPort, s.ep.TransportEndpointInfo.ID.RemotePort, uint32(seg.sequenceNumber), uint32(s.SndUna), seg.xmitCount, COUNTER, s.maxRetries, s.ep.stack.Stats().TCP.Timeouts.Value(), s.ep.stack.Stats().TCP.EstablishedTimedout.Value())'''
    sender.write_text(original.replace(anchor, anchor + "\n\t\t" + witness.replace("COUNTER", counter)))
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
    for w in s["retry_limit_witness"]:
        side = (w["local"], w["remote"])
        role = "server" if side == pair(before) else "peer" if side == pair(peer) else "unknown"
        row["close_witnesses"].append(dict(role=role, segment_transmits=w["xmit"], tested_count=w["count"],
              limit=w["limit"], stack_timeouts=w["timeouts"], established_timedout=w["established_timedout"],
              at_first_gap=bool(gaps) and role == "server" and w["sequence_offset"] == gaps[0]["start"]))
    deadline = bool(s["client_error"])
    request = s["requested"]
    valid = (endpoints and s["operation"] == "response_read" and request == 2_800_000 and s["writer_requests"] == 3
             and s["server_requested"] == request and not s["capture_limit"] and s["sent_packets"] > 0
             and s["client_timed_out"] == deadline and 0 <= s["client_read"] <= request
             and s["missing_wire_bytes"] == s["never_sent_bytes"] + s["sent_undelivered_bytes"]
             and (s["loss"] == "burst") == (s["dropped_packets"] > 0)
             and all(w["role"] != "unknown" and w["tested_count"] > w["limit"] for w in row["close_witnesses"]))
    if deadline:
        valid = valid and s["loss"] == "burst" and s["ms"] >= 29_900 and s["client_read"] < request
    else:
        valid = valid and s["client_read"] == request and s["missing_wire_bytes"] == 0 and not gaps and bool(s["server_after"]) and bool(s["peer_after"])
    row.update(endpoints_matched=endpoints, valid=valid)
    return row


def run(arm):
    blobs = sources(arm)
    row = REPORT["arms"][arm] = {"rack_snd_blobs": blobs, "observed_sender_blob": git("hash-object", FILES[1]), "samples": []}
    save()
    bep = TMP / ("rto-" + arm + ".bep")
    cmd = [str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g",
           "test", "--config=x86_64", "--jobs=3", "--test_output=errors", "--color=no",
           "--nocache_test_results", "--strategy=TestRunner=local", "--test_timeout=860",
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
                    and [(r["loss"], r["seed"]) for r in rows] == TRIALS and all(r["valid"] for r in rows)
                    and counts == [(row["complete"], deadlines)])
    save()
    print(arm, "complete", row["complete"], "deadlines", deadlines, "valid", row["valid"], flush=True)


assert git("rev-parse", "HEAD^") == HARNESS and git("rev-parse", HARNESS + "^") == BASE
assert git("rev-parse", PR + "^") == BASE and git("rev-parse", CONTROL + "^") == BASE
assert git("hash-object", "pkg/tcpip/transport/tcp/endpoint.go") == "77313c575cc9af85321a637c9341fe8b2dd74081"
for arm in ("pr", "timeout_counting"):
    try:
        run(arm)
    except (KeyError, ValueError, IndexError, AssertionError, OSError, subprocess.CalledProcessError) as ex:
        REPORT["arms"].setdefault(arm, {})["measurement_error"] = type(ex).__name__
        save()
assert all(REPORT["arms"].get(a, {}).get("valid") for a in ("pr", "timeout_counting"))
