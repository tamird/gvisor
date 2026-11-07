"""Verify the original failed fdbased control from its test log and XML."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

BLOB = re.compile(r"(bytestream://(?:remote|app)\.buildbuddy\.io/(?:[A-Za-z0-9._-]+/)*?)(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/([0-9]+)")
LIMIT = 256 * 1024
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


EXPECTED = {
    "TestSeqpacketWorkerShutdown": "FAIL",
    "TestSeqpacketWorkerShutdown/PeerEOF": "FAIL",
    "TestSeqpacketWorkerShutdown/DetachAfterEOF": "FAIL",
    "TestSeqpacketWorkerShutdown/AdministrativeStop": "PASS",
}
INV = "f9367db2-e9da-4009-be7f-3e7521dcc254"
LABEL = "//pkg/tcpip/link/fdbased:fdbased_test"
result = {"original_run": 42345885372, "original_artifact": 13563970470, "invocation": INV, "owner": LABEL,
          "baseline": "8a120ab6c04ad9b8007be155d16ce0f98b0b87df", "test_source": "ac9ce7c6cedddadcba30b2b183ccf7068ab3e551",
          "verified": False}
try:
    data = original_files(INV, LABEL, result)
    log = data["test.log"]
    states = {}
    unexpected = 0
    for tc in ET.fromstring(data["test.xml"]).iter("testcase"):
        top, slash, sub = tc.attrib["name"].partition("/")
        name = top.rsplit(".",1)[-1] + slash + sub
        if name not in EXPECTED:
            unexpected += 1
            continue
        if name in states:
            raise ValueError("duplicate-owned-case")
        states[name] = ("SKIP" if tc.find("skipped") is not None else
                        "FAIL" if tc.find("error") is not None or tc.find("failure") is not None else "PASS")
    result["xml"] = states
    result["unknown_cases"] = unexpected
    result["log_matches"] = {name: ("--- "+state+": "+name+" (").encode() in log for name,state in EXPECTED.items()}
    result["native_amd64"] = len(re.findall(rb"(?m)^NATIVE_PEER_WORKER=x86_64\s*$",log)) == 1
    result["early_notification"] = b"ran before the occupied worker returned" in log
    result["missing_packet"] = b"delivered packets = [1], want [1 2]" in log
    result["queue_at_eof_mismatch"] = b"queued packets at EOF =" in log
    result["queue_after_detach_mismatch"] = b"queued packets after detach =" in log
    result["verified"] = (result["native_amd64"] and states==EXPECTED and not unexpected and all(result["log_matches"].values()) and
                          result["early_notification"] and result["missing_packet"] and not result["queue_at_eof_mismatch"] and
                          not result["queue_after_detach_mismatch"])
except (OSError, http.client.HTTPException, ValueError, KeyError, ET.ParseError) as err:
    message=str(err)
    allowed={"response-over-limit","action-pages-over-limit","test-action-not-unique","unexpected-test-file-uri",
             "test-file-digest-mismatch","duplicate-owned-case"}
    result["error"] = message if message in allowed or re.fullmatch(r"rpc-status-[0-9]{3}",message) else type(err).__name__
text=json.dumps(result,indent=2)+"\n"
assert len(text)<8192 and KEY not in text
(Path(os.environ["RUNNER_TEMP"])/"fdbased-original.json").write_text(text)
raise SystemExit(not result["verified"])
