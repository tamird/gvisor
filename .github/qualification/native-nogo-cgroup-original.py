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
    record["targetActions"] = []
    for target in (label, label + "_nogo"):
        response = rpc("GetTarget", {"invocationId": uid, "targetLabel": target})
        groups = response.get("targetGroups", [])
        record.setdefault("targets", {})[target] = [(g.get("status"), len(g.get("targets",[]))) for g in groups]
        for group in groups:
            for t in group.get("targets", []):
                for event in t.get("actionEvents", []):
                    action = event.get("action", {})
                    if action.get("type") != "GoCompilePkg":
                        continue
                    rec = {"mnemonic":action.get("type"),"exit":action.get("exitCode"),"success":action.get("success",False)}
                    detail = action.get("failureDetail",{})
                    rec["failureClass"] = {k: v.get("code") for k,v in detail.items() if isinstance(v,dict)}
                    rec["message"] = list(lines(detail.get("message","").encode()))
                    for name in ("stderr","stdout"):
                        f = action.get(name,{})
                        uri = f.get("uri","")
                        rec[name] = {"uriScheme":uri.partition(":")[0],"size":f.get("length")}
                        if f.get("contents"):
                            rec[name].update(evidence(base64.b64decode(f["contents"])))
                        else:
                            valid = re.fullmatch(r"bytestream://(?:remote|cache)\.buildbuddy\.io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)",uri)
                            if valid and int(valid[2])<4194304:
                                try:
                                    b = request("/api/v1/GetFile", {"uri":uri})
                                    assert len(b)==int(valid[2]) and hashlib.sha256(b).hexdigest()==valid[1]
                                    rec[name].update(evidence(b))
                                except (ValueError,OSError,AssertionError) as err:
                                    rec[name]["lookupError"] = type(err).__name__
                    record["targetActions"].append(rec)
    try:
        chunk = rpc("GetEventLogChunk", {"invocationId": uid, "minLines": 30000})
        buf = base64.b64decode(chunk.get("buffer",""))
        record["logEvidence"] = evidence(buf)
        record["logComplete"] = not chunk.get("previousChunkId")
        record["labelMentions"] = buf.count(label.encode())
        txt = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "",buf.decode(errors="replace")).splitlines()
        selected = [i for i,l in enumerate(txt) if label in l or ("ERROR:" in l and "cgroup" in l)]
        kinds = []
        for i in selected:
            for l in txt[max(i-3,0):i+8]:
                q=l.strip()
                if not q or key in q or re.search(r"token|grpcs:|https?:|api-key|bearer|authorization|secret|\s--|\(cd |^exec |^env |PATH=",q,re.I):
                    continue
                if len(q)<=420 and not q.startswith("["):
                    kinds.append(q)
        record["cgroupContext"] = list(dict.fromkeys(kinds))[:25]
    except (OSError,ValueError) as err:
        record["logLookupError"] = type(err).__name__
finally:
    record["finished"] = datetime.datetime.now(datetime.UTC).isoformat()
    data = json.dumps(record, indent=2)
    assert key and key not in data
    out.write_text(data+"\n")
