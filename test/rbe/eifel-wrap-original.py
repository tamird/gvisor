"""Recover phase diagnostics for two known empty invocations."""
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

key = os.environ["BUILDBUDDY_API_KEY"]
out = {}
for uid in ("12f3907e-fa03-4326-a915-a2f11846ad3c", "9d422a70-59a7-4259-9a7f-f62f737f39f8"):
    request = urllib.request.Request("https://app.buildbuddy.io/rpc/BuildBuddyService/GetEventLogChunk",
        data=json.dumps({"invocationId": uid, "minLines": 500}).encode(),
        headers={"Content-Type":"application/json","x-buildbuddy-api-key":key},method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            log = base64.b64decode(json.load(response).get("buffer", "")).decode(errors="replace")
    except urllib.error.HTTPError as e:
        log = f"RPC status {e.code}: " + e.read(768).decode(errors="replace")
    lines=[]
    for line in log.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line)
        if any(x in line.lower() for x in ("x-buildbuddy", "api_key", "api-key", "authorization", "--", "bearer", "credential", "token=", "secret", "command line of", "environment")): continue
        if line.startswith(("ERROR:", "WARNING:", "RPC status", "Error", "  * ", "Caused by:", "           Traceback", " * Traceback")) or any(x in line.lower() for x in ("exec format", "external error", "exit code", "failed to start", "rpc error", "remote execution", "runc run", "invalid argument", "module resolution","yanked","no such package","pkix","handshakeexception","not found","visibility","no repository visible","registry","resolution failed","certificate","repository mapping","bzlmod","failed to load","unsupported option","unrecognized option")):
            lines.append(line[:700])
    assert key and key not in json.dumps(lines)
    out[uid]={"bytes":len(log), "diagnostic":lines[-30:]}
(Path(os.environ["RUNNER_TEMP"])/"original-resolution.json").write_text(json.dumps(out,indent=2)+"\n")
