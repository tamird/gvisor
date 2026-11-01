"""Identify the first original failure, emitting only public helper names."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re

from cas_inputs import blob, fetch, rpc

INV = "f9a06f1c-cc5f-4b9c-b518-128f1505231b"
ACTION = "28ae53bea672c865eada319c9fb0c64f84085a01d2779651eb8fa3191be9c5de"
OUT = Path(os.environ["RUNNER_TEMP"]) / "helper-evidence.json"
KNOWN = ["auto-config-h.sh", "check-experimental-syms.sh", "gen-build-mk.sh",
         "gen-config-h.sh", "gen-pmdinfo-cfile.sh", "relpath.sh", "symlink-drivers-solibs.sh"]
row = {"status": "INCOMPLETE", "original_build": "FAIL", "invocation": INV,
       "action": ACTION + "/376", "original_zip": "bf037c746ba25e52b1493f6f0221a31c3cda70b2d6c55634c0ce72ad568c40c0",
       "source": "933f7013f9480b0ee99ea148b1d8caeddc5e8a80", "streams": {}}


def save():
    OUT.write_text(json.dumps(row, indent=2) + "\n")


def project(data):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", data.decode(errors="replace"))
    relevant = []
    for num, line in enumerate(text.splitlines(), 1):
        for name in KNOWN:
            # The helper must be named on the failure line itself.
            match = re.search(re.escape("buildtools/" + name) +
                              r"""['":\s]+(?:/bin/(?:ba)?sh: bad interpreter: )?(cannot execute: required file not found|No such file or directory|not found|Permission denied)""", line)
            if match:
                relevant.append({"line": num, "name": name, "error": match[1]})
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "failed_helpers": relevant,
            "config_recipe_127": bool(re.search(r"rte\.sdkconfig\.mk:\d+: [^\n]*rte_config\.h\] Error 127", text)),
            "config_stage": "generating custom config" in text}


def main():
    # The earlier Make consumer's original 454-byte stdout exercises the
    # same authenticated blob reader before inspecting the DPDK failure.
    control = {"hash": "0ccf3724c232b6b9400e92503f9a817a6a236bc5a2dfc984b35aeec18867b2ec",
               "sizeBytes": "454"}
    previous = blob(digest=control, invocation="57ccdecd-07cf-4034-8ff6-298bce8a8031")
    assert re.findall(rb"^\[PASS\] (\w+)\s*$", previous, re.M) == [
        b"version", b"hello", b"substref", b"functions", b"fail_propagation", b"parallel"]
    row["known_original_read"] = control
    save()
    # First prefer the exact action's two streams, with the original digest.
    try:
        matches = rpc("GetExecution", {"executionLookup": {"invocationId": INV, "actionDigestHash": ACTION},
                                       "inlineExecuteResponse": True}).get("execution", [])
        action, = [a for a in matches if a.get("actionDigest", {}).get("hash") == ACTION]
        response = action["executeResponse"]
        result = response["result"]
        assert action["stage"] in (4, "COMPLETED") and result["exitCode"] == 2 and not response.get("cachedResult")
        row["fresh_exit"] = result["exitCode"]
        save()
        for stream in ["stdout", "stderr"]:
            digest = result.get(stream + "Digest", {})
            if int(digest.get("sizeBytes", 0)) > 0:
                data = blob(digest=digest, invocation=INV)
            else:
                data = base64.b64decode(result.get(stream + "Raw", ""), validate=True)
            row["streams"][stream] = project(data)
            save()
    except Exception as exc:
        row["action_read_error"] = type(exc).__name__
        save()
    if not any(s["failed_helpers"] for s in row["streams"].values()):
        chunks = []
        body = {"selector": {"invocation_id": INV}}
        for _ in range(24):
            response = json.loads(fetch("api/v1/GetLog", body))
            log = response["log"]
            # GetLog authenticates the requested UUID but currently returns
            # only log.contents. Reject an identifier if it contradicts the request.
            ids = log.get("id", {})
            if ids:
                assert ids.get("invocationId", ids.get("invocation_id")) == INV
            chunks.append(log.get("contents", ""))
            token = response.get("nextPageToken", response.get("next_page_token", ""))
            if not token:
                break
            body["page_token"] = token
        else:
            raise ValueError("incomplete original log")
        row["streams"]["bazel"] = project("".join(chunks).encode())
    row["status"] = "RECOVERED" if any(s["failed_helpers"] for s in row["streams"].values()) else "INCOMPLETE"


save()
try:
    main()
except Exception as exc:
    row["read_error"] = type(exc).__name__
save()
raise SystemExit(row["status"] != "RECOVERED")
