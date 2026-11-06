"""Recover four original test outputs without running Bazel."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

CASES = (
    "TestRepeatedSynInSynRcvd", "TestDuplicateSynTimestampHalfSpace",
    "TestStaleRepeatedSynGetsACK", "TestRepeatedSynRenewsDeadline",
)
ARMS = {
    "native": ("3b65b1d9-bf6e-4593-b14e-2971cb106fbc",
               "//test/packetimpact/tests:tcp_acceptable_ack_syn_rcvd_native_test"),
    "fixed": ("246ff058-ddda-438d-9aef-5cbc2416a346",
              "//test/packetimpact/tests:tcp_acceptable_ack_syn_rcvd_netstack_test"),
}
BLOB = re.compile(r"(bytestream://(?:remote|app)\.buildbuddy\.io/(?:[A-Za-z0-9._-]+/)*?)(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/([0-9]+)")
LIMIT = 4 * 1024 * 1024
KEY = os.environ["BUILDBUDDY_API_KEY"]
assert KEY


def rpc(method, payload, limit):
    assert method in ("GetAction", "GetFile")
    conn = http.client.HTTPSConnection("app.buildbuddy.io", timeout=25)
    try:
        conn.request("POST", "/api/v1/" + method, json.dumps(payload), {
            "content-type": "application/json", "x-buildbuddy-api-key": KEY,
        })
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("rpc-status-" + str(response.status))
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("response-over-limit")
        return data
    finally:
        conn.close()


def field(obj, snake, camel):
    return obj.get(snake, obj.get(camel))


def original_files(invocation, label, row):
    selector = {"invocation_id": invocation, "target_label": label}
    req = {"selector": selector}
    matches = []
    for _ in range(4):
        page = json.loads(rpc("GetAction", req, 1024 * 1024))
        for action in page.get("action", []):
            if field(action.get("id", {}), "invocation_id", "invocationId") != invocation:
                continue
            if field(action, "target_label", "targetLabel") != label:
                continue
            files = {f.get("name"): f.get("uri", "") for f in action.get("file", [])
                     if f.get("name") in ("test.log", "test.xml")}
            if set(files) == {"test.log", "test.xml"}:
                matches.append(files)
        token = field(page, "next_page_token", "nextPageToken")
        if not token:
            break
        req = {"selector": selector, "page_token": token}
    else:
        raise ValueError("action-pages-over-limit")
    row["matching_actions"] = len(matches)
    if len(matches) != 1:
        raise ValueError("test-action-not-unique")
    data = {}
    row["files"] = {}
    for name in ("test.log", "test.xml"):
        match = BLOB.fullmatch(matches[0][name])
        if not match or not 0 < int(match[3]) <= LIMIT:
            raise ValueError("unexpected-test-file-uri")
        # Transport compression is optional. The digest covers the original bytes.
        uri = match[1] + "blobs/" + match[2] + "/" + match[3]
        blob = rpc("GetFile", {"uri": uri}, int(match[3]))
        actual = hashlib.sha256(blob).hexdigest()
        if actual != match[2] or len(blob) != int(match[3]):
            raise ValueError("test-file-digest-mismatch")
        row["files"][name] = {"sha256": actual, "bytes": len(blob)}
        data[name] = blob
    return data


def examine(data, row):
    found = {}
    outcomes = {"passed": 0, "failed": 0, "skipped": 0}
    for case in ET.fromstring(data["test.xml"]).iter("testcase"):
        name = case.get("name", "").rsplit("/", 1)[-1]
        state = "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            state = "failed"
        elif case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed":
            state = "skipped"
        outcomes[state] += 1
        if name in CASES:
            if name in found:
                raise ValueError("duplicate-testcase")
            found[name] = state
    row["xml_cases"] = found
    row["xml_totals"] = outcomes
    log = data["test.log"]
    row["log_cases"] = {name: bool(re.search(rb"--- PASS: " + name.encode() + rb"\s", log)) for name in CASES}
    timers = re.findall(rb"DUPSYN_TIMER before_repeat_us=(\d+) requested_reply_us=(\d+) timed_reply_us=(\d+)", log)
    stamps = re.findall(rb"DUPSYN_TS initial=(\d+):(\d+) requested=(\d+):(\d+) timed=(\d+):(\d+) total=(\d+) child_rtt_us=(\d+)", log)
    if len(timers) == len(stamps) == 1:
        row["timer_us"] = [int(n) for n in timers[0]]
        row["timestamps"] = [int(n) for n in stamps[0]]
        a, p1, b, p2, c, p3, total, _ = row["timestamps"]
        row["timestamp_check"] = p1 == p2 == p3 == 1100 and total == 2 and 0 < (b-a) % 2**32 < 2**31 and 0 < (c-b) % 2**32 < 2**31
    row["verified"] = (found == dict.fromkeys(CASES, "passed") and not outcomes["failed"]
                       and not outcomes["skipped"] and row.get("timestamp_check") is True)


result = {"original_run": 42251069517, "arms": {}}
for arm, (invocation, label) in ARMS.items():
    row = result["arms"][arm] = {"invocation": invocation, "label": label, "verified": False}
    try:
        examine(original_files(invocation, label, row), row)
    except (OSError, http.client.HTTPException, ET.ParseError, ValueError, KeyError) as err:
        # Report only controlled diagnostics. Neither RPC bodies nor raw logs leave this process.
        known = {"response-over-limit", "action-pages-over-limit", "test-action-not-unique",
                 "unexpected-test-file-uri", "test-file-digest-mismatch", "duplicate-testcase"}
        msg = str(err)
        row["error"] = msg if msg in known or re.fullmatch(r"rpc-status-[0-9]{3}", msg) else type(err).__name__
encoded = json.dumps(result, indent=2) + "\n"
assert KEY not in encoded
(Path(os.environ["RUNNER_TEMP"]) / "original-duplicate-syn.json").write_text(encoded)
raise SystemExit(not all(row["verified"] for row in result["arms"].values()))
