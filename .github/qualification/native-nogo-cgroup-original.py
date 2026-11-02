"""Read the two original failed cgroup actions; never execute a build."""
import base64
import datetime
import hashlib
import json
import os
import re
from http.client import HTTPSConnection
from pathlib import Path

uid = "1c14ce87-518f-4e52-bedf-45e54d2390b5"
label = "//runsc/cgroup:cgroup_test"
key = os.environ["BUILDBUDDY_API_KEY"]
out = Path(os.environ["RUNNER_TEMP"]) / "nogo-cgroup-original.json"
record = {"source": "8e13db5ad62802add68356df383e4a581e8eb303", "invocation": uid,
          "label": label, "originalFailures": 2, "actions": [], "logDiagnostics": []}
upper = 32 * 1024 * 1024


def request(path, body):
    conn = HTTPSConnection("app.buildbuddy.io", timeout=50)
    try:
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "x-buildbuddy-api-key": key})
        with conn.getresponse() as r:
            if r.status != 200:
                raise ValueError("HTTP " + str(r.status))
            data = r.read(upper + 1)
            if len(data) > upper:
                raise ValueError("truncated")
            return data
    finally:
        conn.close()


def rpc(name, body):
    return json.loads(request("/rpc/BuildBuddyService/" + name, body))


def lines(data):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", data.decode(errors="replace"))
    for l in text.splitlines():
        s = l.strip()
        if len(s) > 750 or key in s or re.search(r'https?:|token|api-key|authorization|bearer|remote.header|bes.header|secret|^\s*(?:cd|exec|env) ', s, re.I):
            continue
        if re.search(r'\.go:\d+:|(?:GoCompilePkg|compilepkg:).*?(?:error|failed|unrecognized|cannot|missing)|(?:not found|no such file|no space left|permission denied|fatal error|cannot find|not declared|not executable|exec format error|missing strict dependenc|undefined:|signal: killed|scheduling failed|execution denied|action must run locally)|^\s*(?:have|want) \(', s, re.I):
            yield s[:550]


def evidence(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "diagnostics": list(dict.fromkeys(lines(data)))[:45]}


def stream(result, name):
    raw = result.get(name + "Raw")
    if raw:
        return evidence(base64.b64decode(raw))
    d = result.get(name + "Digest") or {}
    h, n = d.get("hash", ""), int(d.get("sizeBytes") or 0)
    if re.fullmatch(r"[0-9a-f]{64}", h) and 0 < n <= 4*1024*1024:
        data = request("/api/v1/GetFile", {"uri": f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"})
        assert len(data) == n and hashlib.sha256(data).hexdigest() == h
        return evidence(data)
    return {"bytes": 0}


try:
    for target in [label, "@@" + label]:
        try:
            body = {"executionLookup": {"invocationId": uid, "targetLabel": target}, "inlineExecuteResponse": True}
            rows = rpc("GetExecution", body).get("execution", [])
            for row in rows:
                if row.get("targetLabel", "").lstrip("@") != label or row.get("actionMnemonic") != "GoCompilePkg":
                    continue
                response = row.get("executeResponse") or {}
                result = response.get("result") or {}
                status = row.get("status") or {}
                if not (int(row.get("exitCode") or 0) or int(status.get("code") or 0)):
                    continue
                a = {"stage": row.get("stage"), "exit": row.get("exitCode"), "status": status.get("code",0),
                     "action": row.get("actionDigest"), "start": row.get("executedActionMetadata",{}).get("executionStartTimestamp"),
                     "finish": row.get("executedActionMetadata",{}).get("executionCompletedTimestamp"),
                     "statusDiagnostic": list(lines(status.get("message","").encode()))}
                for name in ("stderr","stdout"):
                    try:
                        a[name] = stream(result,name)
                    except (OSError,ValueError,AssertionError) as err:
                        a[name] = {"lookupError": type(err).__name__}
                if a not in record["actions"]:
                    record["actions"].append(a)
        except (OSError,ValueError) as err:
            record.setdefault("lookupErrors",[]).append(type(err).__name__)
    try:
        chunk = rpc("GetEventLogChunk", {"invocationId": uid, "minLines": 30000})
        buf = base64.b64decode(chunk.get("buffer",""))
        record["logEvidence"] = evidence(buf)
        record["logComplete"] = not chunk.get("previousChunkId")
        record["labelMentions"] = buf.count(label.encode())
    except (OSError,ValueError) as err:
        record["logLookupError"] = type(err).__name__
finally:
    record["finished"] = datetime.datetime.now(datetime.UTC).isoformat()
    data = json.dumps(record, indent=2)
    assert key and key not in data
    out.write_text(data+"\n")
