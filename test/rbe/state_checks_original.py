"""Retrieve only the original state race and ARM analyzer test outputs."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request
import uuid
import xml.etree.ElementTree as ET

KEY = os.environ["BUILDBUDDY_API_KEY"]
OUT = Path(os.environ["RUNNER_TEMP"]) / "state-original-checks"
OUT.mkdir(exist_ok=True)
SOURCE = "4cebf830f4b7128ff5ed8f0e24401bbeb1b7980b"
CARRIER = "3f1b69b80b6fbcb5c6386e5cfd5daebaa331bc82"
ORIGINALS = {
    "race": ("13a6a3a3-9897-461a-b859-58c916db2c77", ["//pkg/state:state_test", "//pkg/state/tests:tests_test"]),
    "arm-analyzers": ("2754fa08-ea07-44df-9124-6b100dabe00a", ["//pkg/state:state_nogo", "//pkg/state:state_test_nogo", "//pkg/state/tests:tests_nogo", "//tools/go_stateify:stateify_nogo"]),
}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def request(method, body, raw=False):
    path = "/rpc/BuildBuddyService/SearchInvocation" if method == "SearchInvocation" else "/api/v1/" + method
    req = urllib.request.Request(
        "https://app.buildbuddy.io" + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": KEY},
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
        content = response.read(32 * 1024 * 1024 + 1)
    assert len(content) <= 32 * 1024 * 1024
    return content if raw else json.loads(content)

def pages(method, body, field):
    result = []
    for _ in range(5):
        value = request(method, body)
        result.extend(value.get(field, []))
        token = value.get("nextPageToken", value.get("next_page_token"))
        if not token:
            return result
        body = dict(body, page_token=token)
    raise ValueError("Invocation metadata exceeded bounded pagination")

def value(item, camel, snake):
    return item.get(camel, item.get(snake))

def identifier(item):
    return str(uuid.UUID(value(item.get("id", item), "invocationId", "invocation_id")))

def save(name, obj):
    data = (json.dumps(obj, indent=2) + "\n").encode()
    assert KEY.encode() not in data and b"x-buildbuddy-api-key" not in data.lower()
    (OUT / (name + ".json")).write_bytes(data)


def diagnostics(raw):
    rows = raw.decode(errors="replace").splitlines()
    kept = []
    for line in rows:
        if re.search(r"api.key|authorization|cookie|https?://|remote_header", line, re.I):
            continue
        if re.search(r"WARNING: DATA RACE|^Read at |^Previous (read|write)|^Write at |^Goroutine .* created at:|^--- FAIL:|^FAIL|race detected|panic:|fatal error:|exec format error|No such file|cannot execute|checklocks|checkescape|\.go:[0-9]+|unexpected findings|Unexpected findings", line):
            kept.append(line[:700])
    return kept[:120]


def recover(name, invocation, owners):
    result = {"retrieval": "INCOMPLETE", "original_run": 41335098309,
              "source": SOURCE, "carrier": CARRIER, "invocation": invocation, "owners": []}
    save(name, result)
    try:
        info, = pages("GetInvocation", {"selector": {"invocation_id": invocation}}, "invocation")
        assert identifier(info) == invocation
        sha = value(info, "commitSha", "commit_sha")
        assert sha in (None, "", SOURCE, CARRIER)
        result["source_metadata"] = sha
        targets = pages("GetTarget", {"selector": {"invocation_id": invocation}}, "target")
        assert {r["label"] for r in targets} == set(owners)
        for row in targets:
            assert identifier(row) == invocation
            item = {"label": row["label"], "original_status": row.get("status"),
                    "timing": row.get("timing"), "reports": []}
            result["owners"].append(item)
            save(name, result)
            actions = pages("GetAction", {"selector": {"invocation_id": invocation, "target_label": row["label"]}}, "action")
            for action in actions:
                assert identifier(action) == invocation and value(action, "targetLabel", "target_label") == row["label"]
                for file in action.get("file", []):
                    if file.get("name") not in ("test.log", "test.xml"):
                        continue
                    uri = file.get("uri", "")
                    match = re.fullmatch(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)", uri)
                    assert match, "Original test output lacks a known digest-bound URI"
                    raw = request("GetFile", {"uri": uri}, raw=True)
                    assert len(raw) == int(match[2]) and hashlib.sha256(raw).hexdigest() == match[1]
                    assert KEY.encode() not in raw
                    report = {"name": file["name"], "sha256": match[1], "bytes": len(raw), "diagnostics": diagnostics(raw)}
                    if file["name"] == "test.xml":
                        xml = ET.fromstring(raw)
                        report["suite_attributes"] = [{k: x.get(k) for k in ("name", "tests", "failures", "errors", "skipped", "time") if x.get(k) is not None} for x in xml.iter("testsuite")]
                        report["failed_cases"] = [x.get("name") for x in xml.iter("testcase") if any(x.find(k) is not None for k in ("error", "failure"))]
                    item["reports"].append(report)
                    save(name, result)
        result["retrieval"] = "RECOVERED" if all(x["reports"] for x in result["owners"]) else "INCOMPLETE"
    except Exception as err:
        result["error_type"] = type(err).__name__
    result["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save(name, result)
    print(name, result["retrieval"])
    return result["retrieval"] == "RECOVERED"


if __name__ == "__main__":
    outcomes = [recover(name, uid, owners) for name, (uid, owners) in ORIGINALS.items()]
    raise SystemExit(0 if all(outcomes) else 1)
