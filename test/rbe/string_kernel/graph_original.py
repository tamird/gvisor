"""Classify the original Go-helper failure; read only its fixed official invocation."""

from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re

ID = "7d757ad3-29ac-445a-bb63-891dfd72e1b1"
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
    if line.startswith(("ERROR:", "WARNING: Download from ")) and any(phrase in line for phrase in ("no such package","Error downloading","Checksum","failed","Unable to","Could not")):
        if not any(word in line.lower() for word in ("secret", "authorization", "header", "password", "api-key", "token=")):
            line = re.sub(r"https?://[^ ]+", lambda m: m.group().split("?")[0], line)
            selected.append(line[:900])
record = {"id":ID, "bytes":len(text), "sha256":sha256(text.encode()).hexdigest(),"diagnostics":list(dict.fromkeys(selected))[:9]}
(Path(os.environ["RUNNER_TEMP"])/"graph-origin.json").write_text(json.dumps(record,indent=2)+"\n")
assert record["diagnostics"], "no original dependency diagnostic"
