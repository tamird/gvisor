"""Compare a refused TCP piggyback and an accepted one on the same test link."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

work = Path(os.environ["RUNNER_TEMP"]) / "delayed-ack"
base = "f05cd8923fb8aa27186834df01d7433085db41f1"
fix = "26513de7a5026a086c35d791fcd9c1596984722c"
sender = "pkg/tcpip/transport/tcp/snd.go"
fixture = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_delayed_ack_test.go")
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_delayed_ack_test"
refused = "TestDelayedACKAfterRejectedResponse"
accepted = "TestDelayedACKSentWithData"
reason = "missing pure ACK at the original deadline after the rejected response"
receipt = {"owner": owner, "base": base, "fix": fix, "arms": {}}


def git(*args):
    return subprocess.check_output(["git", *map(str, args)], text=True).strip()


def save():
    (work / "result.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def instrument():
    original = fixture.read_text()
    start = original.index("func " + refused + "(")
    end = original.index("func " + accepted + "(", start)
    case = original[start:end]
    markers = {
        "return &tcpip.ErrNoBufferSpace{}": (
            't.Logf("ACKQ reject %d %d %d", pkt.Data().Size(), h.Flags(), h.AckNumber())'
        ),
        "entry, readable :=": "start := clock.NowMonotonic()",
        "clock.Advance(delayedACKTimeout/2 - time.Nanosecond)": (
            't.Logf("ACKQ refused %d %d", clock.NowMonotonic().Sub(start).Nanoseconds(), errors.Value()-before)'
        ),
        "clock.Advance(time.Nanosecond)": (
            't.Logf("ACKQ silent %d", clock.NowMonotonic().Sub(start).Nanoseconds())'
        ),
        "checker.IPv4(t, b,": (
            'wire := header.TCP(header.IPv4(b.AsSlice()).Payload())\n'
            't.Logf("ACKQ received %d %d %d %d", clock.NowMonotonic().Sub(start).Nanoseconds(), '
            'len(wire.Payload()), wire.Flags(), wire.AckNumber())'
        ),
    }
    for anchor, observation in markers.items():
        assert case.count(anchor) == 1, anchor
        case = case.replace(anchor, observation + "\n\t" + anchor)
    fixture.write_text(original[:start] + case + original[end:])
    receipt["test_blob"] = git("rev-parse", f"{fix}:{fixture}")
    receipt["instrumented_test_blob"] = git("hash-object", fixture)
    save()


def output(entry):
    uri = urlsplit(entry["uri"])
    assert uri.scheme == "file" and not uri.netloc
    data = Path(unquote(uri.path)).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    assert entry.get("digest", sha) == sha
    assert int(entry.get("length", len(data))) == len(data)
    return data, sha


def run(arm, revision):
    git("restore", "--source=" + revision, "--", sender)
    row = receipt["arms"][arm] = {"sender_blob": git("hash-object", sender)}
    assert row["sender_blob"] == git("rev-parse", revision + ":" + sender)
    assert git("hash-object", fixture) == receipt["instrumented_test_blob"]
    bep = work / (arm + ".jsonl")
    flags = [
        "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
        "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
        "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
        "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
        "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
        "--nocache_test_results", "--test_sharding_strategy=disabled", "--flaky_test_attempts=1",
        "--test_timeout=180", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v",
        f"--test_filter=^({refused}|{accepted})$", "--build_event_json_file=" + str(bep),
        "--test_output=errors", "--color=no", "--curses=no", "--noshow_progress",
    ]
    log = work / (arm + ".console")
    with log.open("wb") as f:
        cmd = [work / "bazelisk", "--nosystem_rc", "--nohome_rc", "test", *flags, owner]
        row["exit"] = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
    save()
    events = [json.loads(line) for line in bep.open()]
    rows = [(e["id"]["testResult"], e["testResult"]) for e in events
            if e.get("id", {}).get("testResult", {}).get("label") == owner and "testResult" in e]
    if len(rows) != 1:
        row["diagnostic"] = [s[:350] for s in log.read_text(errors="replace").splitlines()
                             if "ERROR:" in s or re.search(r"\.go:\d+:\d+:", s)][:12]
        save()
    (identity, result), = rows
    row.update(configuration=identity["configuration"]["id"], status=result["status"],
               run=int(identity.get("run", 1)), shard=int(identity.get("shard", 1)),
               attempt=int(identity.get("attempt", 1)), strategy=result.get("executionInfo", {}).get("strategy"),
               cached=bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely")))
    files = {item["name"]: item for item in result["testActionOutput"]}
    data, row["log_sha256"] = output(files["test.log"])
    xml, row["xml_sha256"] = output(files["test.xml"])
    text = data.decode(errors="replace")
    row["markers"] = re.findall(r"ACKQ ([a-z]+(?: [0-9]+)+)\s*$", text, re.M)
    row["deadline_failures"] = len(re.findall(re.escape(reason) + r"\s*$", text, re.M))
    starts = re.findall(r"^\s*=== RUN\s+(\S+)\s*$", text, re.M)
    ends = re.findall(r"^\s*--- (PASS|FAIL|SKIP): (\S+) \(", text, re.M)
    row["go"] = {name: status for status, name in ends}
    row["xml"] = {}
    for case in ElementTree.fromstring(xml).iter("testcase"):
        key = case.attrib["name"]
        assert key not in row["xml"]
        skipped = (case.find("skipped") is not None or case.get("status") in {"notrun", "skipped"}
                   or case.get("result") in {"suppressed", "skipped"})
        failed = case.find("failure") is not None or case.find("error") is not None
        row["xml"][key] = "SKIP" if skipped else "FAIL" if failed else "PASS"
    save()
    want = {refused: "FAIL" if arm == "published" else "PASS", accepted: "PASS"}
    markers = ["reject 3 24 1790", "refused 20000000 1", "silent 39999999"]
    if arm == "fixed":
        markers.append("received 40000000 0 16 1790")
    assert sorted(starts) == sorted(want) and len(ends) == 2
    assert row["go"] == row["xml"] == want
    assert row["markers"] == markers and row["deadline_failures"] == int(arm == "published")
    assert not row["cached"] and row["strategy"] == "linux-sandbox"
    assert row["run"] == row["shard"] == row["attempt"] == 1
    assert row["status"] == ("FAILED" if arm == "published" else "PASSED")
    assert row["exit"] == (3 if arm == "published" else 0)
    assert [e["finished"]["exitCode"].get("code", 0) for e in events if "finished" in e] == [row["exit"]]
    row["verified"] = True
    save()


assert git("rev-parse", "HEAD") == fix and git("rev-parse", "HEAD^") == base
assert git("rev-parse", "HEAD^{tree}") == "c9edc26c6fc8caf4f10cd02c1739248b26ed0033"
assert git("rev-parse", f"{fix}:{fixture}") == "ae64ec624ca05e3cffe3f8a5fdb9cc7b26a94829"
receipt["link_hook_blob"] = git("rev-parse", "HEAD:pkg/tcpip/transport/tcp/testing/context/context.go")
assert receipt["link_hook_blob"] == "1f472259c9bab126c0f55e7e78a601884c40dc29"
instrument()
for arm, revision in (("published", base), ("fixed", fix)):
    try:
        run(arm, revision)
    except Exception as error:
        receipt["arms"][arm]["observer_error"] = f"{type(error).__name__}: {error}"[:360]
        save()
assert all(row.get("verified") for row in receipt["arms"].values()), "check original per-arm receipts"
receipt["verified"] = True
save()
