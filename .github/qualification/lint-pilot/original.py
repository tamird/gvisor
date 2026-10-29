"""Inspect only the original supplier invocation. Do not expose raw log lines."""

import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

UUID = "30a0c288-d397-4549-8699-7462bad30276"
q = {"selector": {"invocationId": UUID}}
key = os.environ.pop("ORIGINAL_KEY")
out = {"invocation": UUID, "pages": [], "markers": []}
try:
    for _ in range(12):
        req = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetLog",
            data=json.dumps(q).encode(), headers={"Content-Type":"application/json", "x-buildbuddy-api-key":key})
        with urllib.request.urlopen(req, timeout=15) as reply:
            data=reply.read(8*1024*1024+1)
        if len(data)>8*1024*1024:
            out["bounded"]=True
            break
        item=json.loads(data)
        lines=item.get("log",{}).get("contents","")
        if not isinstance(lines,str):
            break
        out["pages"].append({"bytes":len(lines.encode()),"sha256":hashlib.sha256(lines.encode()).hexdigest()})
        for line in lines.lower().splitlines():
            tokens=tuple(t for t in ("test-setup.sh","generate-xml","/usr/bin/env","/bin/bash","no such file or directory","executable file not found","failed to start") if t in line)
            if tokens and list(tokens) not in out["markers"]:
                out["markers"].append(list(tokens))
        token=item.get("nextPageToken")
        if not token:
            break
        q["pageToken"]=token
except (OSError, ValueError, KeyError) as e:
    out["read_error"]=type(e).__name__
    if isinstance(e,urllib.error.HTTPError): out["status"]=e.code
(Path(os.environ["RUNNER_TEMP"])/"original-launch.json").write_text(json.dumps(out,sort_keys=True,indent=2)+"\n")
