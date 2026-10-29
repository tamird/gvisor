"""Read only diagnostics for the two recorded standard-library actions."""
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

source = "eaf429600f44b9cb375fb8fed2000739faf87c99"
iid = "55589219-16ca-4482-a4ec-36bcd25a42b4"
api = "https://app.buildbuddy.io"
token = os.environ.pop("READ_EXISTING_INVOCATION")
class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Keep the invocation credential on the single configured HTTPS origin.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(NoRedirect())
def fetch(path, request=None):
    hdr = {"x-buildbuddy-api-key": token, "Content-Type": "application/json"}
    data = None if request is None else json.dumps(request).encode()
    with opener.open(urllib.request.Request(api + path, data=data, headers=hdr), timeout=60) as res:
        return res.read()
def rpc(method, request):
    return json.loads(fetch("/rpc/BuildBuddyService/" + method, request))
def sanitize(x):
    return "\n".join(l.replace(token, "[redacted]") for l in x.splitlines()
                     if not re.search("(?i)(api[-_]key|authorization|remote_header|bes_header|credential|bearer )", l))
def rpc_error(e):
    return sanitize(e.read(600).decode(errors="replace") if isinstance(e, urllib.error.HTTPError) else str(e))
def member(obj, key):
    return obj.get(key, obj.get(re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()))
def stderr_result(er):
    result = member(er, "result") or {}
    digest = member(result, "stderrDigest")
    raw = member(result, "stderrRaw")
    line = "inline"
    if raw:
        content = base64.b64decode(raw)
    elif digest:
        bs = "bytestream://remote.buildbuddy.io/blobs/{}/{}".format(digest["hash"], member(digest, "sizeBytes"))
        try:
            content = fetch("/file/download?" + urllib.parse.urlencode({"invocation_id": iid, "bytestream_url": bs}))
            line = "cas"
        except Exception as e:
            content = b""
            line = "stderr read: " + rpc_error(e)
    else:
        content = b""
        line = "none"
    return {"exit": member(result, "exitCode") or 0, "stderr_digest": digest, "stderr_source": line,
            "stderr": sanitize(content.decode(errors="replace"))[:10000]}
report = {"source": source, "invocation": iid, "actions": [], "log": [], "rpc_errors": []}
try:
    raw = rpc("GetEventLogChunk", {"invocationId": iid, "minLines": 10000, "type": "BUILD_LOG"})
    lines = base64.b64decode(raw.get("buffer", "")).decode(errors="replace").splitlines()
    positions = [i for i,l in enumerate(lines) if any(q in l for q in ("GoStandardLibraryAnalysis", "Analyzing Go Standard Library", "exit status 34", "error loading", "error running"))]
    indices = sorted({j for i in positions for j in range(max(0,i-2),min(len(lines),i+17))})
    report["log"] = sanitize("\n".join(lines[j] for j in indices))[:26000].splitlines()
    report["log_lines_examined"] = len(lines)
except Exception as e:
    report["rpc_errors"].append("log: " + rpc_error(e))
try:
    response = rpc("GetExecution", {"executionLookup":{"invocationId":iid,"targetLabel":"//tools/nogo:stdlib"},"inlineExecuteResponse":True})
    for e in (response.get("execution",[]) or []):
        er=member(e,"executeResponse") or {}
        report["actions"].append({"id":member(e,"executionId"),"digest":member(e,"actionDigest"),
            "command":sanitize(member(e,"commandSnippet") or "")[:750], "exit":member(e,"exitCode") or 0,
            "status":member(e,"status") or {"code":0}, "attempt_status":member(er,"status"),
            "result":stderr_result(er)})
except Exception as e:
    report["rpc_errors"].append("execution: " + rpc_error(e))
out=Path(os.environ["RUNNER_TEMP"])/"nogo-evidence.json"
out.write_text(json.dumps(report, indent=2).replace(token, "[redacted]"))
print("log_lines:",report.get("log_lines_examined"),"actions:",len(report["actions"]),"read_errors:",report["rpc_errors"])
if not report["log"] and not report["actions"]:
    raise SystemExit("No analyzer diagnostics were available")
