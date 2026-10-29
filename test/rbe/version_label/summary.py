import json
from pathlib import Path
import re
import sys
from datetime import datetime, timezone


def main():
    root = Path(sys.argv[1])
    out = root / "result"
    out.mkdir(exist_ok=True)
    rows = []
    for path in sorted((root / "native").glob("*")):
        row = {"command": path.name.split("-")[0], "targets": [], "tests": []}
        for line in path.read_text(errors="replace").splitlines():
            if not line:
                continue
            event = json.loads(line)
            if "started" in event:
                row.update(invocation=event["started"]["uuid"], start=event["started"].get("startTime"))
            if "finished" in event:
                code=event["finished"]["exitCode"]
                row.update(exit=code.get("code",0) if code.get("name")=="SUCCESS" else code.get("code"), name=code.get("name"), finish=event["finished"].get("finishTime"))
            ident = event.get("id", {})
            if "targetCompleted" in ident:
                row["targets"].append([ident["targetCompleted"]["label"], event["completed"].get("success", False)])
            if "testResult" in ident:
                result = event["testResult"]
                row["tests"].append({"label":ident["testResult"]["label"], "status":result["status"], "cached":result.get("cachedLocally",False),
                    "execution":result.get("executionInfo",{}).get("strategy"),
                    "files": {p["name"]:p.get("uri","").split("/blobs/")[-1] for p in result.get("testActionOutput",[]) if p["name"] in ("test.xml","test.log")}})
        rows.append(row)
    diag = {}
    for key in ("controls", "ordinary", "release", "restore"):
        p = root / (key + ".log")
        if not p.exists():
            continue
        safe = []
        for line in p.read_text(errors="replace").splitlines():
            if re.search(r"secret|api.key|password|authorization|token=|remote_header|bes_header|x-buildbuddy", line, re.I):
                continue
            if re.search(r"error|failed|warning:.*main thread|invalid RELEASE|must contain a digit|reported=|not found|permission denied|no such file|Traceback|AssertionError|VERSION-CHECK", line, re.I):
                safe.append(line[:900])
        diag[key] = list(dict.fromkeys(safe))[-35:]
    (out / "native.json").write_text(json.dumps(rows, indent=2)+"\n")
    (out / "diagnostics.json").write_text(json.dumps(diag,indent=2)+"\n")
    (out / "verified-at.txt").write_text(datetime.now(timezone.utc).isoformat()+"\n")


if __name__ == "__main__":
    main()
