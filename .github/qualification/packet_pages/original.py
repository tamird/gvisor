"""Recover only the original failed packet-ring TestRunner output."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid

HOST = "remote.buildbuddy.io"
TARGET = "//test/rbe/packet_pages:on_64k"
STAMP = {"updatedAfter": "2026-11-06T04:35:00Z", "updatedBefore": "2026-11-06T04:38:04Z"}
KEY = os.environ.pop("BUILDBUDDY_API_KEY")
receipt = {"target": TARGET, "window": STAMP, "status": "INCOMPLETE"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def post(endpoint, body, limit):
    assert endpoint in ("rpc/BuildBuddyService/SearchExecution", "rpc/BuildBuddyService/GetExecution",
                        "api/v1/GetFile")
    receipt["stage"] = endpoint
    req = urllib.request.Request("https://" + HOST + "/" + endpoint, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-buildbuddy-api-key": KEY})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=35) as resp:
        result = resp.read(limit + 1)
    if len(result) > limit:
        raise ValueError("Response limit")
    return result


def diagnostics(raw):
    text = raw.decode(errors="replace")
    markers = ["Guest page size: 65536", "6.8.0-138-generic-64k",
               "tpBlockSize: 2101248 is not page aligned, pagesize: 65536",
               "PACKET_RING setup arch=arm64 page=65536 bytes=2162688",
               "PACKET_RING delivered 40 sequential UDP markers", "Guest did not report a completed test",
               "Passing guest omitted test XML", "stock unexpectedly accepted the 64 KiB packet ring"]
    found = []
    for line in text.splitlines():
        if re.search(r"command not found|No such file or directory|Permission denied|Exec format error|"
                     r"unshare:|expected a private loopback namespace|TestPacketMMapDeliversUDP|"
                     r"phase=guest_status_read|: (exec|cd):", line, re.I):
            if KEY not in line and not re.search(r"(?i)token|password|authorization|secret|api.key|https?://", line):
                found.append(re.sub(r"[^a-zA-Z0-9 ./:_+,()=\-\[\]]", "", line)[:220])
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "markers": [s for s in markers if s in text], "diagnostics": found[:12]}


def lookup():
    dims = [{"dimension": {"execution": kind}, "value": value} for kind, value in
            [("TARGET_LABEL_EXECUTION_DIMENSION", TARGET), ("ACTION_MNEMONIC_EXECUTION_DIMENSION", "TestRunner")]]
    search = json.loads(post("rpc/BuildBuddyService/SearchExecution",
                             {"query": {**STAMP, "dimensionFilter": dims}, "count": 2}, 2_000_000))
    records = search.get("execution", [])
    receipt["matches"] = len(records)
    if len(records) != 1 or search.get("nextPageToken"):
        return
    item = records[0]
    ex = item["execution"]
    assert ex["targetLabel"] == TARGET and ex["actionMnemonic"] == "TestRunner" and ex["exitCode"] == 1
    inv = item["invocationMetadata"]["id"]
    assert str(uuid.UUID(inv)) == inv
    exec_id = ex["executionId"]
    digest = ex["actionDigest"]["hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    receipt["invocation"] = inv
    receipt["action"] = digest
    result = json.loads(post("rpc/BuildBuddyService/GetExecution",
                             {"executionLookup": {"invocationId": inv, "executionId": exec_id, "targetLabel": TARGET},
                              "inlineExecuteResponse": True}, 5_000_000))
    row, = result["execution"]
    assert row["executionId"] == exec_id and row["actionDigest"]["hash"] == digest
    action = row.get("executeResponse", {}).get("result", {})
    if action.get("exitCode") != 1:
        return
    for output in action.get("outputFiles", []):
        path = output["path"]
        if path != "test.log" and not path.endswith("/testlogs/test/rbe/packet_pages/on_64k/test.log"):
            continue
        d = output["digest"]
        sha = d["hash"]
        size = int(d["sizeBytes"])
        assert re.fullmatch(r"[0-9a-f]{64}", sha) and 0 < size <= 4_000_000
        raw = (base64.b64decode(output["contents"], validate=True) if "contents" in output else
               post("api/v1/GetFile", {"uri": f"bytestream://{HOST}/blobs/{sha}/{size}"}, size))
        assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha
        receipt["original_test_log"] = diagnostics(raw)
        receipt["status"] = "RECOVERED"
        break


try:
    lookup()
except urllib.error.HTTPError as exc:
    receipt["http_status"] = exc.code
except (AssertionError, KeyError, ValueError, TimeoutError, OSError) as exc:
    receipt["read_error_type"] = type(exc).__name__
finally:
    Path(os.environ["RUNNER_TEMP"], "packet-original.json").write_text(json.dumps(receipt, indent=2) + "\n")
