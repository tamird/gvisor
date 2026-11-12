"""Recover public dependency diagnostics from one existing fork invocation."""

import http.client
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

INVOCATION = "d44f1d48-650e-4d4a-af0b-c7e4d8b4d02c"
PUBLIC_HOSTS = {"github.com", "release-assets.githubusercontent.com", "mirror.bazel.build",
                "bcr.bazel.build", "archive.ubuntu.com", "ports.ubuntu.com", "kernel.ubuntu.com",
                "snapshot.ubuntu.com", "storage.googleapis.com", "dl.google.com"}
result = {"invocation": INVOCATION, "errors": []}


def public_url(match):
    url = urlsplit(match.group())
    if url.hostname not in PUBLIC_HOSTS:
        return "<external host>"
    return url.hostname + (url.path if url.hostname != "release-assets.githubusercontent.com" else "")


request = bytes([18, len(INVOCATION)]) + INVOCATION.encode() + bytes([32, 208, 15, 40, 1])
conn = http.client.HTTPSConnection("remote.buildbuddy.io", timeout=25)
try:
    key = os.environ["BUILDBUDDY_API_KEY"]
    conn.request("POST", "/rpc/BuildBuddyService/GetEventLogChunk", body=request,
                 headers={"Content-Type": "application/proto", "x-buildbuddy-api-key": key})
    response = conn.getresponse()
    result["status"] = response.status
    if response.status == 200:
        content = response.read(4 << 20).decode(errors="replace").replace(key, "<redacted>")
        content = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", content)
        for line in content.splitlines():
            if not re.search(r"error:|error in |failed to (?:download|fetch|build)|timed out|404 not found|could not (?:get|download|resolve)|checksum|no such (?:package|target)|repository .+ instantiated at", line, re.I):
                continue
            if any(word in line.lower() for word in ("--remote_header", "authorization:", "credential=", "bearer ")):
                continue
            line = re.sub(r"https?://[^\s'\"<>\]]+", public_url, line)
            line = "".join(c for c in line if c.isprintable())[:1000]
            if line not in result["errors"]:
                result["errors"].append(line)
            if len(result["errors"]) >= 60:
                break
finally:
    conn.close()
    (Path(os.environ["RUNNER_TEMP"]) / "guest-setup.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result), flush=True)
