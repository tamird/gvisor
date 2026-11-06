"""Keep the packaging evidence even if one invocation fails."""

import json
from pathlib import Path
import re
import sys
import uuid

base = Path(sys.argv[1])
owner = "//vdso:distro_wrapper_test"
r = {"product": "14f43c8fc0e83728c1d8cc1fb92662fd1e098ab7", "verified": False,
     "exit": int((base / "exit").read_text()) if (base / "exit").is_file() else None}
log = (base / "run.log").read_text(errors="replace") if (base / "run.log").exists() else ""
if (base / "events").is_file():
    for line in (base / "events").open():
        ev = json.loads(line)
        if "started" in ev:
            r["invocation"] = str(uuid.UUID(ev["started"]["uuid"]))
        data = ev.get("testResult")
        if data and ev["id"]["testResult"]["label"] == owner:
            r["test"] = data["status"]
            r["cached"] = bool(data.get("cachedLocally") or data.get("executionInfo", {}).get("cachedRemotely"))
for line in log.splitlines():
    if "DISTRO-WRAPPER {" in line:
        r["fixture"] = json.loads(line.split("DISTRO-WRAPPER ", 1)[1])
r["diagnostics"] = [line[:280] for line in log.splitlines() if re.search(r"(Error:|ERROR:|not found|FAILED:|AssertionError|FileNotFoundError|exec format error)", line)][:9]
cases = r.get("fixture", {}).get("cases", {})
outcomes = {k:v.get("exit") for k,v in cases.items()}
r["verified"] = (r["exit"] == 0 and r.get("test") == "PASSED" and r.get("cached") is False and
                 r.get("fixture", {}).get("route") == "ENETUNREACH" and
                 outcomes == {"recipe-amd64": 0, "external-amd64": 0, "recipe-injected": 2,
                              "external-injected": 1, "external-optional-data": 0, "source-label-alone": 1}
                 and all(cases[n]["data_error"] for n in ("recipe-injected", "external-injected")))
(base / "report.json").write_text(json.dumps(r, indent=2) + "\n")
