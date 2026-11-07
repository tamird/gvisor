"""Check the actual help disposition and the two local, uncached Bazel test results."""

from pathlib import Path
import json
import os
import re
import subprocess

out = Path(os.environ["RUNNER_TEMP"]) / "buildkite-offline"
result = json.loads((out/"help.json").read_text())
result["requested_head"] = "30143217adf5ade390febe1a48447b334c874c2e"
result["bazel_exit"] = int((out/"exit").read_text())
result["tests"] = {}
owners = {"//tools/buildkite:buildkite_test": 25, "//tools/buildkite:offline_observation_test": 3}
for line in (out/"bep").open():
    ev = json.loads(line)
    if ev.get("id",{}).get("testResult",{}).get("label") not in owners or "testResult" not in ev: continue
    owner = ev["id"]["testResult"]["label"]
    test = ev["testResult"]
    raw = (Path("bazel-testlogs/tools/buildkite") / owner.split(":")[1] / "test.log").read_text()
    row = {"status":test["status"], "strategy":test.get("executionInfo",{}).get("strategy"),
           "cached":bool(test.get("cachedLocally") or test.get("executionInfo",{}).get("cachedRemotely")),
           "count":int(re.search(r"(?m)^Ran (\d+) tests? in ",raw)[1]),
           "ok":bool(re.search(r"(?m)^OK\s*$",raw))}
    result["tests"][owner] = row
    if owner.endswith("offline_observation_test"):
        result["observations"] = [json.loads(x[len("OBSERVATION="):]) for x in raw.splitlines() if x.startswith("OBSERVATION=")]
(out/"result.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
assert result["bazel_exit"] == 0 and result["tests"].keys() == owners.keys()
assert all(row["count"] == owners[o] and row["ok"] and row["status"] == "PASSED" and not row["cached"] and row["strategy"]=="linux-sandbox" for o,row in result["tests"].items())
assert len(result["observations"]) == 3
