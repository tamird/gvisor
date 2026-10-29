import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from helpers import BAD_BZL, names, report, rows

scratch = Path(os.environ["RUNNER_TEMP"])
result = {"source": "ea7ed498af5815422760211302edbc14c58ed3f2", "tree": "da7569535bf1f666948d3ed6ab0715b63ffc1f4b", "verdict": "INCOMPLETE", "phases": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def verify():
    assert subprocess.check_output(["git", "log", "-1", "--format=%H %T"], text=True).strip().split() == [result["source"], result["tree"]]
    sources = set(subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")) - {""}
    for mode, expected in (("clean", 0), ("dirty", 4)):
        phase = result["phases"][mode] = {"start": now(), "actions": {}}
        if mode == "dirty":
            path = Path(BAD_BZL)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('"""Native supplier probe."""\n\nX=[1,2]\n')
            sources.add(BAD_BZL)
        frame = scratch / (mode + "-spawns.json")
        cmd = ["bazel", "--bazelrc=" + os.environ["SUPPLIER_RC"], "build",
               "--host_platform=@lint_supplier_host//:scratch", "--extra_execution_platforms=@lint_supplier_host//:scratch",
               "--platforms=//tools/bazeldefs:linux_arm64", "--noremote_accept_cached", "--action_env=BUILDIFIER_CONFIG=example",
               "--execution_log_json_file=" + str(frame), "//:buildifier"]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        phase.update(finish=now(), bazel_exit=p.returncode)
        log = p.stdout.decode(errors="replace")
        phase["links"] = [line.split("https://app.buildbuddy.io/invocation/")[1].strip() for line in log.splitlines() if "https://app.buildbuddy.io/invocation/" in line and "?" not in line]
        # Never upload raw logs or credential material.
        phase["errors"] = [x[:400] for x in log.splitlines() if (x.startswith("ERROR:") or "# reformat" in x) and "key" not in x.lower() and "https:" not in x][:8]
        assert (p.returncode == 0) == (expected == 0), phase
        row, = [row for row in rows(frame) if row.get("mnemonic") == "BuildifierCheck"]
        report(phase, "BuildifierCheck", row, *names(sources, "BuildifierCheck"), expected, log)
    result["verdict"] = "PASS"


try:
    verify()
finally:
    result["verified"] = now()
    (scratch / "native-label.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
