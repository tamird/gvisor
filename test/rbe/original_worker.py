"""Save only the failed package action's diagnostic from an original invocation."""
import base64
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request


def rpc(method, request):
    try:
        req = urllib.request.Request(
            f"https://app.buildbuddy.io/rpc/BuildBuddyService/{method}",
            data=json.dumps(request).encode(),
            headers={"Content-Type": "application/json",
                     "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        return {"requestError": safe(err.read(512).decode(errors="replace"))}


def safe(line):
    line = line.replace(os.environ["BUILDBUDDY_API_KEY"], "[redacted]")
    if re.search(r"remote.header|bes.header|authorization|Bearer|api.key", line, re.I):
        return "[omitted credential-bearing line]"
    return line[:1200]


def first(d, name, snake):
    return d.get(name, d.get(snake))


def main():
    inv, destination = sys.argv[1:]
    records = {"invocation": inv, "execution": [], "diagnostic": []}
    for label in ("//pkg/abi/linux:linux", "@@//pkg/abi/linux:linux"):
        r = rpc("GetExecution", {"executionLookup": {"invocationId": inv, "targetLabel": label}})
        if "requestError" in r:
            records["executionError"] = r["requestError"]
            break
        for ex in r.get("execution", []):
            status = ex.get("status", {})
            if not status.get("code") and not first(ex,"exitCode","exit_code"):
                continue
            records["execution"].append({
                "mnemonic": first(ex,"actionMnemonic","action_mnemonic"),
                "target": first(ex,"targetLabel","target_label"),
                "digest": first(ex,"actionDigest","action_digest"),
                "code": status.get("code"),
                "message": safe(status.get("message", "")),
                "exit": first(ex,"exitCode","exit_code"),
            })
        if records["execution"]:
            break
    chunk = ""
    for _ in range(4):
        r = rpc("GetEventLogChunk", {"invocationId": inv, "chunkId": chunk, "minLines": 250})
        if "requestError" in r:
            records["logError"] = r["requestError"]
            break
        cleaned = base64.b64decode(r.get("buffer", "")).decode(errors="replace").splitlines()
        for i, line in enumerate(cleaned):
            if "Remote Execution Failure:" in line and "pkg/abi/linux" in line:
                records["diagnostic"] += [safe(s) for s in cleaned[i:i+7] if s]
        if records["diagnostic"]:
            break
        chunk = first(r,"previousChunkId","previous_chunk_id")
        if not chunk:
            break
    Path(destination).write_text(json.dumps(records, indent=2))
    print(json.dumps(records))
    if not (records["execution"] or records["diagnostic"]):
        print("No extra worker diagnostic was available for this invocation.")


if __name__ == "__main__":
    main()
