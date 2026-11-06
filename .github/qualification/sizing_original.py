"""Read the exact Nginx benchmark invocation's original build failure."""

import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

IID = "b6dc29c2-9dee-4ce5-a1f5-686ef8f9232e"
TARGET = "//test/rbe/encoder_hints:nginx"
HOST = "remote.buildbuddy.io"
KEY = os.environ.pop("BUILDBUDDY_API_KEY")
report = {"invocation": IID, "target": TARGET, "rows": [], "err": []}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def read(name, data, size):
    assert name in ("GetInvocation", "GetEventLogChunk")
    req = urllib.request.Request(f"https://{HOST}/rpc/BuildBuddyService/" + name, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json", "x-buildbuddy-api-key": KEY})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as resp:
        b = resp.read(size + 1)
        if len(b) > size:
            raise ValueError("response limit")
        return json.loads(b)


def safe(line):
    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
    if KEY in line or re.search(r"(?i)(api.key|credential|password|authorization|remote_header|bes_header|cookie|token|secret|sig=)", line):
        return ""
    line = re.sub(r"https?://\S+", "[url]", line)
    return re.sub(r"[^A-Za-z0-9_ =+?.:\-/()\[\],;@!]", "", line)[:450]


try:
    log = read("GetEventLogChunk", {"invocationId": IID, "minLines": 90}, 3_000_000)
    raw = base64.b64decode(log.get("buffer", ""), validate=True)
    text = raw.decode(errors="replace").replace("\r", "\n")
    lines = [safe(v) for v in text.splitlines()]
    marked = [i for i, line in enumerate(lines) if re.search(
        r"(?i)(\berror\b|^FAIL|failed|incompatib|no space|no such|denied|undefined:|invalid|not found|"
        r"undeclared|missing|interrupted|killed|cannot|no matching|timed out|no test targets|not visible)", line)]
    indexes = sorted({j for i in marked for j in range(max(i-1, 0), min(i+3, len(lines)))})
    report["diagnostic"] = list(dict.fromkeys(lines[i] for i in indexes if lines[i]))[-20:]
    report["console_bytes"] = len(raw)
except (urllib.error.HTTPError, OSError, ValueError) as e:
    report["err"].append(["log", getattr(e, "code", type(e).__name__)])

try:
    data = read("GetInvocation", {"lookup":{"invocationId": IID}}, 30_000_000)
    item, = data["invocation"]
    assert item["invocationId"] == IID
    report["exit"] = safe(item.get("bazelExitCode", ""))
    for event in item.get("event", []):
        be = event.get("buildEvent", {})
        ident = be.get("id", {})
        if "action" in be and not be["action"].get("success"):
            action = be["action"]
            who = ident.get("actionCompleted", {})
            report["rows"].append({"action": safe(action.get("type", "")), "owner": safe(who.get("label", "")),
                                   "exit": action.get("exitCode"), "message": safe(action.get("failureDetail",{}).get("message",""))})
        if "aborted" in be or "finished" in be:
            aborted = be.get("aborted", {})
            failure = be.get("finished", {}).get("failureDetail", {})
            msg = safe(aborted.get("description", "") or failure.get("message", ""))
            if msg and len(report.get("aborts", [])) < 8:
                report.setdefault("aborts", []).append(msg)
        if ident.get("testResult",{}).get("label") == TARGET:
            report["test_status"] = be.get("testResult", {}).get("status")
except (urllib.error.HTTPError, KeyError, ValueError, OSError, AssertionError) as e:
    report["err"].append(["invocation", getattr(e, "code", type(e).__name__)])
report["rows"] = report["rows"][:10]
Path(os.environ["RUNNER_TEMP"], "sizing-original.json").write_text(json.dumps(report,indent=2) + "\n")
