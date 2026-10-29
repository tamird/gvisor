"""Classify the original Go-helper failure; read only its fixed official invocation."""

from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re

ID = "aa4c0043-ac29-4807-bb59-aa9e295fea33"
key = os.environ.pop("BUILDBUDDY_API_KEY")
token = "0000"
seen, parts = set(), []
for _ in range(16):
    assert token not in seen
    seen.add(token)
    c = HTTPSConnection("app.buildbuddy.io", timeout=25)
    try:
        c.request("POST", "/api/v1/GetLog", json.dumps({"selector":{"invocation_id":ID},"page_token":token}),
                  {"Content-Type":"application/json","x-buildbuddy-api-key":key})
        with c.getresponse() as r:
            assert r.status == 200, r.status
            b = r.read((2 << 20) + 1)
            assert len(b) <= 2 << 20
        j = json.loads(b)
        parts.append(j.get("log",{}).get("contents",""))
        token = j.get("nextPageToken", j.get("next_page_token",""))
        if not token:
            break
    finally:
        c.close()
else:
    raise ValueError("original log page bound")
text = re.sub(r"\x1b\[[;\d]*[A-Za-z]", "", "\n".join(parts)).replace("\r","\n")
assert text and key not in text
selected = []
for line in text.splitlines():
    if not any(word in line.lower() for word in ("authorization", "secret", "api-key", "header=", "password", "token=", "--remote_header", "--bes_header")) and any(word in line.lower() for word in ("error:", "fatal:", "no such file", "not found", "unrecognized", "unexpected", "syntax error", "permission denied", "unbound variable", "cannot ", "undefined reference", "segmentation", "usage:", "unknown option", "illegal", "not executable")):
        line = re.sub(r"https?://[^ ]+", lambda m: m.group().split("?")[0], line)
        selected.append(line[:850])
record = {"id":ID, "bytes":len(text), "sha256":sha256(text.encode()).hexdigest(),"diagnostics":list(dict.fromkeys(selected))[-24:]}
(Path(os.environ["RUNNER_TEMP"])/"kernel-origin.json").write_text(json.dumps(record,indent=2)+"\n")
assert record["diagnostics"], "no original GNU diagnostic"
