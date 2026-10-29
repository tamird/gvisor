"""Keep only target selection, native success/failure and the built runsc identity."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys


def main() -> None:
    phase, exit_text = sys.argv[1:]
    result = {"phase": phase, "source": os.environ["PRODUCT"], "machine": platform.machine(),
              "make_or_bazel_exit": int(exit_text), "configured": [], "completed": {},
              "failures": [], "process_summary": []}
    work = Path(os.environ["RUNNER_TEMP"])
    bep = work / f"{phase}.jsonl"
    if bep.is_file():
        with bep.open() as stream:
            for line in stream:
                event = json.loads(line)
                ids = event["id"]
                if "targetConfigured" in ids:
                    result["configured"].append(ids["targetConfigured"]["label"])
                if "targetCompleted" in ids and not ids["targetCompleted"].get("aspect"):
                    label = ids["targetCompleted"]["label"]
                    result["completed"][label] = event.get("completed", {}).get("success", False)
                if "buildFinished" in ids:
                    result["bazel_exit"] = event["finished"]["exitCode"]
                if "action" in event and not event["action"].get("success", False):
                    action = event["action"]
                    result["failures"].append({
                        "owner": ids.get("actionCompleted", {}).get("label"),
                        "type": action.get("type"), "exit": action.get("exitCode"),
                        "failure": action.get("failureDetail", {}).get("message", "")[:900]})
                if "buildMetrics" in event:
                    result["actions"] = event["buildMetrics"].get("actionSummary")
    log = (work / f"{phase}.log").read_text(errors="replace")
    result["first_errors"] = [line[:1600] for line in log.splitlines()
                              if line.startswith("ERROR:")][:4]
    result["process_summary"] = [line for line in log.splitlines()
                                 if re.match(r"INFO: [\d,]+ process", line)]
    result["configured"] = sorted(set(result["configured"]))
    valid = (result["make_or_bazel_exit"] == 0 and result.get("bazel_exit", {}).get("name") == "SUCCESS")
    if phase == "runsc" and valid:
        paths = work / "runsc.outputs"
        binaries = [Path(s) for s in paths.read_text().splitlines()
                    if Path(s).name == "runsc" and Path(s).is_file()] if paths.is_file() else []
        if len(binaries) == 1:
            header = binaries[0].read_bytes()
            result["runsc"] = {"size": len(header), "sha256": hashlib.sha256(header).hexdigest(),
                               "machine": int.from_bytes(header[18:20], "little")}
            valid = (header.startswith(b"\x7fELF") and result["runsc"]["machine"] ==
                     {"aarch64": 183, "x86_64": 62}[result["machine"]])
        else:
            valid = False
        valid = valid and result["completed"].get("//runsc:runsc", False)
    if phase == "pkg-analysis" and valid:
        valid = "//pkg/sentry/devices/nvproxy:nvproxy_driver_parity_test" in result["configured"]
    result["complete"] = valid
    (work / "receipts" / f"{phase}.json").write_text(json.dumps(result, sort_keys=True) + "\n")
    if not valid:
        sys.exit(1)


if __name__ == "__main__":
    main()
