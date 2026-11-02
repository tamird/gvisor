"""Keep original build/test status and safe, bounded diagnostics."""
import json
from pathlib import Path
import sys
import re

name, root, code = sys.argv[1:]
root = Path(root)
results = []
identity = {}
bep = root / (name + ".bep")
label = "//test/rbe/m003_pair:" + name
if bep.exists():
    with bep.open() as src:
        for line in src:
            event = json.loads(line)
            if "started" in event:
                identity["uuid"] = event["started"].get("uuid")
            if "finished" in event:
                identity["finish_time_millis"] = event["finished"].get("finishTimeMillis")
            key = event["id"].get("testResult", {})
            if key.get("label") == label:
                r = event["testResult"]
                results.append({"run": key.get("run", 1), "attempt": key.get("attempt", 1), "status": r["status"],
                                "cached": r.get("cachedLocally", False) or r.get("executionInfo", {}).get("cachedRemotely", False)})
errors = []
log = root / (name + ".log")
if log.exists():
    for line in log.read_text(errors="replace").splitlines():
        if re.search(r"(ERROR:|undefined:|not used|FAILED:|invalid operation|not declared|No such file|panic:|AssertionError)", line):
            if not re.search(r"(?i)https?://|grpc|header|token|bearer|password|api.key|credential", line):
                errors.append(line.strip()[:420])
(root / "records" / (name + ".status.json")).write_text(json.dumps({"exit": int(code), "invocation": identity, "attempts": results, "diagnostics": errors[-12:]}, indent=2))
if code == "0":
    assert len(results) == 1 and results[0]["status"] == "PASSED" and not results[0]["cached"], results
