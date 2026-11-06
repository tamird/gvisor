"""Read two original compiler logs without sending their contents to Actions."""
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re

ROOT = Path(os.environ["RUNNER_TEMP"]) / "make-syscalls"
ORIGINAL = ["54be0a28-db02-4b96-858d-70fd6d68d9b7", "c0ea1cac-5dc4-48dc-961a-93477fab6523"]
LOC = re.compile(r"test/util/errno_safe_allocator\.cc:(\d+):(\d+): (?:fatal )?error: (.+)")
report = {"invocations": {}, "matches_pr15368": False}
key = os.environ.pop("BUILDBUDDY_API_KEY")
try:
    for inv in ORIGINAL:
        entry = report["invocations"][inv] = {"diagnostics": [], "classes": [], "pages": 0, "matched": False}
        payload = {"selector": {"invocation_id": inv}}
        received = hashlib.sha256()
        carry = ""
        for _ in range(32):
            con = HTTPSConnection("app.buildbuddy.io", timeout=25)
            try:
                con.request("POST", "/api/v1/GetLog", body=json.dumps(payload),
                            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": key})
                response = con.getresponse()
                entry["http_status"] = response.status
                if response.status != 200:
                    raise RuntimeError("original log read failed")
                raw = response.read(8*1024*1024 + 1)
                assert len(raw) <= 8*1024*1024
            finally:
                con.close()
            page = json.loads(raw)
            text = page["log"]["contents"]
            received.update(text.encode())
            entry["pages"] += 1
            lines = (carry + text).split("\n")
            carry = lines.pop()
            token = page.get("nextPageToken") or page.get("next_page_token")
            if not token:
                lines.append(carry)
            for line in lines:
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).rstrip("\r")
                m = LOC.search(clean)
                if not m:
                    continue
                n, col, msg = m.groups()
                row = {"file": "test/util/errno_safe_allocator.cc", "line": int(n), "column": int(col),
                       "message_sha256": hashlib.sha256(msg.encode()).hexdigest()}
                if int(n) in (39, 40) and ("exception specification" in msg.lower() or "exception specifier" in msg.lower()) and ("noexcept" in msg or "exception specification" in msg.lower()):
                    row["class"] = "delete exception specification"
                if row not in entry["diagnostics"] and len(entry["diagnostics"]) < 8:
                    entry["diagnostics"].append(row)
            if not token:
                break
            payload["page_token"] = token
        else:
            raise RuntimeError("original log exceeded bounded read")
        entry["log_sha256"] = received.hexdigest()
        entry["matched"] = any(d.get("class") == "delete exception specification" for d in entry["diagnostics"])
    report["matches_pr15368"] = all(report["invocations"].get(inv,{}).get("matched") for inv in ORIGINAL)
finally:
    (ROOT/"original.json").write_text(json.dumps(report, indent=2)+"\n")
raise SystemExit(0 if report["matches_pr15368"] else 1)
