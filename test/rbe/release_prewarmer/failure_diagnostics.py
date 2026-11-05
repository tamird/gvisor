"""Read at most two original failed actions per build from the fixed service."""

import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

import qualify as q


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    key = os.environ["ACCESS"]
    assert key and not (q.TMP / "private.bazelrc").exists()
    opener = urllib.request.build_opener(NoRedirect)
    deadline = time.monotonic() + 150
    result = {"source": q.PRODUCT, "scope": "original failed-action metadata only", "builds": {}}

    def fetch(path, payload=None, limit=1024 * 1024):
        remaining = deadline - time.monotonic()
        assert remaining > 0, "Read budget exhausted"
        request = urllib.request.Request("https://app.buildbuddy.io/" + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"x-buildbuddy-api-key": key, "Content-Type": "application/json"})
        with opener.open(request, timeout=min(20, remaining)) as response:
            raw = response.read(limit + 1)
        assert len(raw) <= limit
        return raw

    def read_stream(meta, invocation):
        size = int(meta["sizeBytes"])
        digest = meta["hash"]
        assert re.fullmatch(r"[0-9a-f]{64}", digest) and 0 < size <= 65536
        uri = "bytestream://remote.buildbuddy.io/blobs/" + digest + "/" + str(size)
        raw = fetch("file/download?" + urllib.parse.urlencode({"bytestream_url": uri, "invocation_id": invocation}), limit=65536)
        assert len(raw) == size and hashlib.sha256(raw).hexdigest() == digest
        return raw

    # Recover only the exact previously successful validation bytes. This is
    # independent of new builds and cannot replace their own original statuses.
    previous = result["previous_validation"] = {"artifact": 13455680535,
        "archive_sha256": "335d89e3ad7ad59d450e366c336987d77d4635cd5abfa87bd27a4848ac5bffd4", "members": {}}
    for arch, invocation in (("amd64", "b6d532ee-bb85-4b75-908b-d55616a989fb"),
                             ("arm64", "a0afbdf1-8a6d-4a09-81d0-8d4e8c314c40")):
        item = previous["members"][arch] = {"invocation": invocation, "status": "INCOMPLETE"}
        try:
            raw = read_stream({"hash": "5987bbe9a66a41608f55e323b75dce876fc4f793cb64f3a2f599f028db058677", "sizeBytes": 15}, invocation)
            assert raw == b"libprotoc 35.1\n"
            item.update(status="RECOVERED_ORIGINAL_OUTPUT", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), version="libprotoc 35.1")
        except Exception as error:
            item["error"] = type(error).__name__
        q.save("original-failures", result)
    for name in ("amd64", "arm64"):
        arch = name.removeprefix("generators-")
        output = result["builds"][name] = {"status": "INCOMPLETE", "actions": []}
        try:
            row = json.loads((q.OUT / (name + ".json")).read_text())
            assert row["source"] == q.PRODUCT and row["arch"] == arch
            invocation = row["events"]["invocation"]
            assert invocation == row["requested_invocation"]
            assert re.fullmatch(r"[0-9a-f-]{36}", invocation)
            output["invocation"] = invocation
            selected = [a for a in row.get("actions", []) if (a["exit"] or a["status"]) and a["worker_matches"] and a["runner"] == "remote" and not a["cache"]][:2]
            if not selected:
                output["status"] = "NO_RECORDED_FAILED_REMOTE_SPAWN"
            for witness in selected:
                item = {"owner": witness["label"], "mnemonic": witness["mnemonic"], "digest": witness["digest"], "recorded_exit": witness["exit"]}
                output["actions"].append(item)
                q.save("original-failures", result)
                try:
                    digest = witness["digest"]
                    assert re.fullmatch(r"[0-9a-f]{64}", digest["hash"]) and int(digest["sizeBytes"]) > 0
                    response = json.loads(fetch("rpc/BuildBuddyService/GetExecution", {
                        "executionLookup": {"invocationId": invocation, "actionDigestHash": digest["hash"]},
                        "inlineExecuteResponse": True}))
                    execution, = [e for e in response.get("execution", [])
                        if e.get("actionDigest", {}).get("hash") == digest["hash"]
                        and str(e["actionDigest"]["sizeBytes"]) == str(digest["sizeBytes"])
                        and e.get("stage") in (4, "COMPLETED")]
                    executed = execution["executeResponse"]
                    assert not executed.get("cachedResult")
                    status = executed.get("status", {})
                    item["remote_status"] = {"code": status.get("code", 0),
                        "message": q.failure_tokens(status.get("message", "").encode())}
                    actual = executed.get("result")
                    if actual is None:
                        assert status.get("code", 0) != 0 and witness["status"]
                        item["status"] = "REMOTE_ERROR_WITHOUT_ACTION_RESULT"
                    else:
                        assert actual.get("exitCode", 0) == witness["exit"]
                        item["streams"] = {}
                        for stream in ("stdout", "stderr"):
                            meta = actual.get(stream + "Digest", {})
                            size = int(meta.get("sizeBytes", 0))
                            if size > 65536:
                                assert re.fullmatch(r"[0-9a-f]{64}", meta["hash"])
                                item["streams"][stream] = {"status": "EXCEEDS_READ_BOUND", "digest": {"hash": meta["hash"], "sizeBytes": size}}
                                continue
                            raw = read_stream(meta, invocation) if size else base64.b64decode(actual.get(stream + "Raw", ""), validate=True)
                            assert len(raw) <= 65536
                            item["streams"][stream] = q.failure_tokens(raw)
                        item["status"] = "RECOVERED_ORIGINAL_OUTPUT"
                except Exception as error:
                    item["error"] = type(error).__name__
                q.save("original-failures", result)
            if selected:
                output["status"] = "ORIGINAL_READ_ATTEMPTED"
        except Exception as error:
            output["error"] = type(error).__name__
        q.save("original-failures", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
