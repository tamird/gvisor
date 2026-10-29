"""Recover just published tool diagnostics from the two original invocations."""
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import check as harness

IDS = {"zip": "f1d3ae45-b725-498a-8dba-e0b2697e82db", "stateify": "e84e49d4-c3e1-4c29-b591-c4a163d2196e"}
result = {}
# Same authenticated, fixed-origin API request as native_receipt; never
# persist an API response wholesale or include credentials in diagnostics.
key = os.environ.pop("BUILDBUDDY_API_KEY")
for subject, invocation in IDS.items():
    row = result[subject] = {"id": invocation, "diagnostics": [], "bootstrap_diagnostic": False}
    try:
        token = ""
        for _ in range(20):
            connection = HTTPSConnection("app.buildbuddy.io", timeout=20)
            try:
                connection.request("POST", "/api/v1/GetLog", json.dumps({"selector": {"invocation_id": invocation}, "page_token": token}),
                                   {"Content-Type": "application/json", "x-buildbuddy-api-key": key})
                reply = connection.getresponse()
                if reply.status != 200:
                    raise RuntimeError(f"GetLog status: {reply.status}")
                packet = json.loads(reply.read())
            finally:
                connection.close()
            body = packet.get("log", {}).get("contents", "")
            context = 0
            for line in body.splitlines():
                clean = re.sub(r"\x1b\[[\d;]*[A-Za-z]", "", line).strip()
                if any(x in clean.lower() for x in ("key", "token", "password", "header", "credential", "authorization", "command line", "commandargs")) or key in clean:
                    continue
                if "failed: (Exit " in clean:
                    context = 5
                if not clean or clean.startswith(("INFO:", "DEBUG:", "WARNING:", "Loading", "Analyzing", "(")):
                    continue
                if context or re.search(r"syntax error|unbound variable|not found|no such file|permission denied|unsupported|cannot |: line [0-9]+:|bad substitution|invalid option|/bin/|exec format|unrecognized option|error while loading", clean, re.I):
                    if clean[:500] not in row["diagnostics"] and len(row["diagnostics"]) < 24:
                        row["diagnostics"].append(clean[:500])
                        row["bootstrap_diagnostic"] |= bool(re.search(r"not found|no such file|syntax error|bad substitution|: line [0-9]+:|invalid option|permission denied|exec format|error while loading", clean, re.I))
                    context = max(0, context - 1)
            token = packet.get("next_page_token") or packet.get("nextPageToken")
            if not token:
                break
    except Exception as exc:
        row["error"] = str(exc)
Path(harness.ROOT / "original-diagnostics.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
