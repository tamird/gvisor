"""Keep only the per-case outcomes and contacts with the pause registry."""

import json
import os
from pathlib import Path
import re

tmp = Path(os.environ["RUNNER_TEMP"])
label = "//test/root:crictl_test_2_2_3_owned"
outcomes = {}
for manifest in (tmp / "qualification").glob("invocation-*/events.jsonl.manifest.json"):
    for report in json.loads(manifest.read_text())["reports"]:
        if report.get("identity", {}).get("label") != label or report.get("name") != "test.log":
            continue
        log = (manifest.parent / report["path"]).read_text(errors="replace")
        records = re.finditer(
            r"cri-pause trial=(\S+) ran=(true|false) passed=(true|false) "
            r"pause_pull_error=(true|false) runp_rpc_error=(true|false) registry_connects=(\d+)", log
        )
        for record in records:
            trial, ran, passed, pull, rpc, connects = record.groups()
            outcomes[trial] = dict(ran=ran == "true", passed=passed == "true", pause_pull_error=pull == "true",
                                   runp_rpc_error=rpc == "true", registry_connects=int(connects))
(tmp / "cri-startup-results.json").write_text(json.dumps(outcomes, indent=2) + "\n")
print(json.dumps(outcomes))
if len(outcomes) != 3 or not all(v["ran"] for v in outcomes.values()):
    raise SystemExit("Missing named CRI cases")
