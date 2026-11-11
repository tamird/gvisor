"""Keep the case outcome, elapsed time and fixed startup stages from each trial."""

import json
import os
from pathlib import Path
import re

owner = "//test/root:crictl_test_2_2_3_owned"
case = "TestCrictlUpdateContainerResources"
tmp = Path(os.environ["RUNNER_TEMP"])
trials = {run: {"run": run, "case": "missing"} for run in range(1, 8)}
stages = ("RunPodSandbox started", "assigned sandbox ID", "starting CNI setup",
          "received CNI result", "selected OCI runtime", "generated OCI spec",
          "RunPodSandbox returned", "RunPodSandbox failed")
stamp = r"[0-9TZ.:+-]+"
for manifest in (tmp / "qualification").glob("invocation-*/events.jsonl.manifest.json"):
    base = manifest.parent
    for line in (base / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        match event:
            case {"id": {"testResult": {"label": label, "attempt": 1, "run": run}},
                  "testResult": result} if label == owner and run in trials:
                trials[run]["bazel"] = result["status"]
                trials[run]["cached"] = bool(result.get("cachedLocally")
                                             or result.get("executionInfo", {}).get("cachedRemotely"))
    for report in json.loads(manifest.read_text())["reports"]:
        match report:
            case {"identity": {"label": label, "attempt": 1, "run": run},
                  "name": "test.log", "path": path} if label == owner and run in trials:
                pass
            case _:
                continue
        text = (base / path).read_text(errors="replace")
        trial = trials[run]
        if outcome := re.search(rf"^\s*--- (PASS|FAIL|SKIP): {case} \(([\d.]+)s\)\s*$", text, re.M):
            trial.update(case=outcome[1], seconds=float(outcome[2]))
        trial["runp_deadline_exceeded"] = bool(re.search(
            r"RunPodSandbox from runtime service failed[^\n]*DeadlineExceeded", text))
        if times := re.search(rf"crictl runp ran from ({stamp}) to ({stamp})\s*$", text, re.M):
            trial["client_interval"] = [times[1], times[2]]
        trial["phases"] = re.findall(rf"containerd ({stamp}): ({'|'.join(stages)})\s*$", text, re.M)
out = tmp / "cri-startup-results.json"
out.write_text(json.dumps(list(trials.values()), indent=2) + "\n")
if any(row["case"] == "missing" or row.get("cached") is not False for row in trials.values()):
    raise SystemExit("Missing named cases or uncached execution records")
