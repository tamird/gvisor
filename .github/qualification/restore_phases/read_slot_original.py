"""Read the sole completed Nginx invocation's two original reports; do not execute it."""

import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


ID = "1e61f5df-dde0-4e3a-9950-349abf8d89d6"
OWNER = "//test/rbe/restore_phases:nginx"
MOUNT_SOURCE = "eeeeeb6ac858e1977c1a981bc018ca98ced94dd8"
LINES = re.compile(r"^(?:phase-(?:frame|restore|capture) |slot-(?:child|complete) |Benchmark(?:CaptureRestorePhases|RestorePhases)|goos: |goarch: |pkg: |cpu: |PASS$|[ \t]*[0-9]+[ \t]+[0-9]+(?:[.]\d+)? ns/op|[0-9a-f]{64}  [^\r\n]*[/](?:candidate[.]bin|checkpointfile_test)$)")


def main(out: Path) -> None:
    key = os.environ.pop("ORIGINAL_RECEIPT_KEY")

    def get(method: str, req: dict, limit: int) -> bytes:
        assert method in {"GetInvocation", "GetAction", "GetTarget", "GetFile"}
        conn = HTTPSConnection("app.buildbuddy.io", timeout=40)
        try:
            conn.request("POST", "/api/v1/" + method, json.dumps(req),
                         {"content-type": "application/json", "x-buildbuddy-api-key": key})
            with conn.getresponse() as res:
                assert res.status == 200, (method, res.status)
                body = res.read(limit + 1)
                assert len(body) <= limit
                return body
        finally:
            conn.close()

    def records(method: str, selector: dict, field: str) -> list:
        return json.loads(get(method, {"selector": selector}, 4 << 20)).get(field, [])

    def brief(record: dict) -> dict:
        return {k: record[k] for k in ("id", "label", "targetLabel", "status", "timing", "invocationStatus",
                                       "success", "exitCode", "commitSha", "command", "durationUsec",
                                       "startTimeUsec", "createdAtUsec", "updatedAtUsec") if k in record}

    query = {"invocation_id": ID, "target_label": OWNER}
    invocations = records("GetInvocation", {"invocation_id": ID}, "invocation")
    assert len(invocations) == 1 and invocations[0]["id"]["invocationId"] == ID
    targets = [t for t in records("GetTarget", {"invocation_id": ID, "label": OWNER}, "target") if t.get("label", t.get("targetLabel")) == OWNER]
    actions = [a for a in records("GetAction", query, "action") if a.get("targetLabel") == OWNER]
    choices = []
    for a in actions:
        assert a["id"]["invocationId"] == ID
        files = [f for f in a.get("file", []) if f.get("name") in {"test.log", "test.xml"}]
        if files:
            assert len(files) == 2 and {f["name"] for f in files} == {"test.log", "test.xml"}
            choices.append((a, files))
    assert len(choices) == 1
    action, files = choices[0]
    meta = {"invocation": brief(invocations[0]), "target": [brief(t) for t in targets],
            "action": brief(action), "files": {}}
    for f in files:
        uri = urlsplit(f["uri"])
        assert uri.scheme == "bytestream" and uri.hostname == "remote.buildbuddy.io"
        assert not (uri.username or uri.password or uri.query or uri.fragment)
        match = re.search(r"/blobs/([0-9a-f]{64})/(\d+)$", uri.path)
        assert match and 0 < int(match[2]) <= 8 << 20
        data = get("GetFile", {"uri": f["uri"]}, int(match[2]))
        assert hashlib.sha256(data).hexdigest() == match[1] and len(data) == int(match[2])
        if f.get("hash"):
            assert f["hash"] == match[1]
        if "sizeBytes" in f:
            assert int(f["sizeBytes"]) == len(data)
        meta["files"][f["name"]] = {"sha256": match[1], "size": len(data), "uri": f["uri"]}
        if f["name"] == "test.log":
            lines = [line for line in data.decode().splitlines() if LINES.match(line)]
            (out / "raw-benchmark.log").write_text("\n".join(lines) + "\n")
    (out / "native-meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    # Single status read for the concurrent package handoff. No monitoring loop.
    try:
        mount = records("GetInvocation", {"commit_sha": MOUNT_SOURCE}, "invocation")
        (out / "mount-status.json").write_text(json.dumps(
            [brief(v) for v in mount if v.get("commitSha") == MOUNT_SOURCE], indent=2) + "\n")
    except (AssertionError, KeyError, OSError) as err:
        (out / "mount-status.json").write_text(json.dumps({"query_failed": type(err).__name__}) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
