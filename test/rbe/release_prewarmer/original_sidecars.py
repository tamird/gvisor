"""Recover only the six original stage0 links and their small output blobs."""

import hashlib
import json
import os
import re
import time
import traceback
import urllib.parse
import urllib.request

import full as f
import install_recovery as recovery
import qualify as q
from failure_diagnostics import NoRedirect


def main():
    key = os.environ["ACCESS"]
    assert key and not (q.TMP / "private.bazelrc").exists()
    opener = urllib.request.build_opener(NoRedirect)
    deadline = time.monotonic() + 230
    result = {"status": "INCOMPLETE", "source": q.PRODUCT, "original": recovery.ORIGINAL,
              "scope": "original completed sidecar CppLinks, BEP configuration and full small CAS bytes; no build", "builds": {}}

    def fetch(path, payload=None, limit=1048576):
        remaining = deadline - time.monotonic()
        assert remaining > 0
        request = urllib.request.Request("https://app.buildbuddy.io/" + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"x-buildbuddy-api-key": key, "Content-Type": "application/json"})
        with opener.open(request, timeout=min(30, remaining)) as response:
            raw = response.read(limit + 1)
        assert len(raw) <= limit
        return raw

    for arch in ("amd64", "arm64"):
        row = result["builds"][arch] = {"status": "INCOMPLETE", "links": []}
        try:
            prior = recovery.original(arch)
            invocation = row["invocation"] = prior["events"]["invocation"]
            response = json.loads(fetch("rpc/BuildBuddyService/GetInvocation", {"lookup": {"invocationId": invocation}}, 64 * 1024 * 1024))
            original, = [v for v in response["invocation"] if v["invocationId"] == invocation]
            configs, actions = {}, {}
            for entry in original.get("event", []):
                event = entry.get("buildEvent", {})
                identity = event.get("id", {})
                if "configuration" in event:
                    configs[identity["configuration"]["id"]] = event["configuration"].get("cpu")
                action = identity.get("actionCompleted", {})
                if action.get("label") in f.C_LINKS and event.get("action", {}).get("type") == "CppLink":
                    actions[(action["label"], action["primaryOutput"])] = {
                        "configuration": action["configuration"]["id"],
                        "success": bool(event["action"].get("success")),
                        "exit": event["action"].get("exitCode", 0),
                    }
            del original, response
            for owner, name in f.C_LINKS.items():
                item = {"owner": owner, "file": name, "status": "INCOMPLETE"}
                row["links"].append(item)
                try:
                    response = json.loads(fetch("rpc/BuildBuddyService/GetExecution", {
                        "executionLookup": {"invocationId": invocation, "targetLabel": owner}, "inlineExecuteResponse": True}))
                    execution, = [v for v in response.get("execution", []) if v.get("targetLabel") == owner
                        and v.get("actionMnemonic") == "CppLink" and v.get("stage") in (4, "COMPLETED")]
                    digest = execution["actionDigest"]
                    assert re.fullmatch(r"[0-9a-f]{64}", digest["hash"]) and int(digest["sizeBytes"]) > 0
                    item["action_digest"] = {"hash": digest["hash"], "sizeBytes": int(digest["sizeBytes"])}
                    executed = execution["executeResponse"]
                    assert not executed.get("cachedResult") and executed.get("status", {}).get("code", 0) == 0
                    actual = executed["result"]
                    item["exit"] = actual.get("exitCode", 0)
                    assert item["exit"] == execution.get("exitCode", 0) == 0
                    output, = [v for v in actual.get("outputFiles", []) if v["path"] == execution["primaryOutputPath"]]
                    item["output"] = output["path"]
                    assert output["path"].startswith("bazel-out/") and output["path"].endswith("/" + name.rsplit("/", 1)[-1])
                    assert ".." not in output["path"].split("/")
                    item["bep"] = actions[(owner, output["path"])]
                    assert re.fullmatch(r"[0-9a-f]{64}", item["bep"]["configuration"])
                    item["bep"]["cpu"] = configs[item["bep"]["configuration"]]
                    assert item["bep"]["success"] and item["bep"]["exit"] == 0
                    assert item["bep"]["cpu"] == {"amd64": "k8", "arm64": "aarch64"}[arch]
                    meta = output["digest"]
                    size, sha = int(meta["sizeBytes"]), meta["hash"]
                    assert 0 < size <= 131072 and re.fullmatch(r"[0-9a-f]{64}", sha)
                    assert (size, sha) == (prior["binaries"][name]["bytes"], prior["binaries"][name]["sha256"])
                    uri = "bytestream://remote.buildbuddy.io/blobs/" + sha + "/" + str(size)
                    raw = fetch("file/download?" + urllib.parse.urlencode({"bytestream_url": uri, "invocation_id": invocation}), limit=131072)
                    assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha
                    item.update(status="PASS", bytes=size, sha256=sha, full_bytes_hashed=True, execution="ORIGINAL_UNCACHED_CPP_LINK",
                                worker_basis="ORIGINAL_COMPACT_ALL_SPAWNS_MATCH_PIN", worker_pin=prior["worker_pin"])
                except Exception as error:
                    item.update(error=type(error).__name__, line=traceback.extract_tb(error.__traceback__)[-1].lineno)
                q.save("original-sidecars", result)
            if len(row["links"]) == 3 and all(v["status"] == "PASS" for v in row["links"]):
                row["status"] = "PASS"
        except Exception as error:
            row.update(error=type(error).__name__, line=traceback.extract_tb(error.__traceback__)[-1].lineno)
        q.save("original-sidecars", result)
    if all(v["status"] == "PASS" for v in result["builds"].values()):
        result["status"] = "PASS"
    q.save("original-sidecars", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
