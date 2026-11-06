"""Read bounded observations from the two in-process TCP stack replays."""

import json
import os
from pathlib import Path
import re
import subprocess

BASE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
FIX = "1b9768b199524246fe8e4fa66a0b623069778780"
FILES = ["pkg/tcpip/transport/tcp/rack.go", "pkg/tcpip/transport/tcp/snd.go"]
LABEL = "//test/tcp_recovery_replay:heavy_loss"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "rto-diagnostics"
OUT.mkdir(exist_ok=True)
REPORT = {"prior_qualification": {"run": 42239956382, "artifact": 13516995551, "sha256":"1ae04a22c8d15ce038ba228c696d039cb10e58ca04970fafbcb311dc99ab240b"}, "base": BASE, "fix": FIX, "reporter": "f72f18f2f7fe0e5ea4151b51ce3f67d68b3b05e2", "arms": {}}


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def save():
    (OUT / "receipt.json").write_text(json.dumps(REPORT, indent=2) + "\n")


assert git("rev-parse", "HEAD^") == BASE
assert git("rev-parse", FIX + "^") == BASE
for arm, rev in (("master", BASE), ("pr", FIX)):
    git("restore", "--source=" + rev, "--worktree", "--", *FILES)
    hashes = [git("hash-object", name) for name in FILES]
    assert hashes == [git("rev-parse", rev + ":" + name) for name in FILES]
    # The reviewed sender sources are verified above. Read the values on its
    # real early-return path after the existing EstablishedTimedout increment.
    sender = Path(FILES[1])
    original = sender.read_text()
    anchor = "if seg != nil && seg.xmitCount > s.maxRetries {\n\t\ts.ep.stack.Stats().TCP.EstablishedTimedout.Increment()"
    assert original.count(anchor) == 1
    witness = r'''fmt.Printf("TCP_RETRY_LIMIT "+`{"local":%d,"remote":%d,"sequence":%d,"length":%d,"una":%d,"nxt":%d,"xmit":%d,"limit":%d,"timeouts":%d,"established_timedout":%d}`+"\n", s.ep.TransportEndpointInfo.ID.LocalPort, s.ep.TransportEndpointInfo.ID.RemotePort, uint32(seg.sequenceNumber), seg.logicalLen(), uint32(s.SndUna), uint32(s.SndNxt), seg.xmitCount, s.maxRetries, s.ep.stack.Stats().TCP.Timeouts.Value(), s.ep.stack.Stats().TCP.EstablishedTimedout.Value())'''
    sender.write_text(original.replace(anchor, anchor+"\n\t\t"+witness))
    command = [
        str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g",
        "test", "--config=x86_64", "--jobs=3", "--test_output=all", "--color=no",
        "--nocache_test_results", "--strategy=TestRunner=local", "--test_timeout=860",
        "--test_arg=-test.v", "--test_env=TCP_REPLAY_ARM=" + arm,
        "--run_under=/usr/bin/unshare --user --map-root-user --net --", LABEL,
    ]
    if arm == "pr":
        prior=REPORT["arms"]["master"]["samples"]
        matched=next((s for s in prior if s["client_timed_out"]),None)
        command.insert(-1, "--test_env=TCP_REPLAY_SEED="+str(matched["seed"] if matched else 4))
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    result = {"source": rev, "rack_snd_blobs": hashes, "observed_sender_blob":git("hash-object",FILES[1]), "samples": [], "timelines": [], "diagnostics": []}
    REPORT["arms"][arm] = result
    in_timeline = False
    for line in p.stdout:
        m = re.match(r"^TCP_SAMPLE arm=" + arm + r" (\{.*\})", line)
        if m:
            result["samples"].append(json.loads(m[1]))
            save()
            in_timeline = False
            continue
        if line.startswith("TCP_TIMELINE arm=" + arm):
            in_timeline = True
        if in_timeline and re.search(r"(?:^TCP_TIMELINE|wire trace at the server|gVisor sender state|^  \+|more 100 ms rows|path drops toward the client:)", line):
            if len(result["timelines"]) < 380:
                result["timelines"].append(line.strip()[:480])
        counts = re.match(r"^TCP_COUNTS arm=" + arm + r" complete=(\d+) deadline=(\d+)", line)
        if counts:
            result["complete"], result["deadlines"] = map(int, counts.groups())
        if re.search(r"ERROR:|error:|panic:|undefined:|\.go:\d+:|failed to|FAIL|timeout exceeded", line, re.IGNORECASE):
            if len(result["diagnostics"]) < 36:
                result["diagnostics"].append(line.strip()[:420])
    result["exit"] = p.wait()
    cases = result["samples"]
    result["valid"] = (
        result["exit"] == 0 and bool(cases) and all(s["loss"] == "burst" for s in cases)
        and len(cases) == result.get("complete", -20) + result.get("deadlines", -20)
    )
    save()
    print(arm, result["exit"], "samples", len(cases), "diagnostics", result["diagnostics"][:6], flush=True)
assert all(a["valid"] for a in REPORT["arms"].values())
