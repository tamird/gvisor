import collections
import json
from pathlib import Path
import subprocess
import sys
import os

ROOT = Path(os.environ["RUNNER_TEMP"]) / "llvm-source-presubmit"
RESULT = ROOT / "results"

def run(arch):
    guest = json.loads((RESULT / f"{arch}.json").read_text())
    old = json.loads((ROOT / "old.json").read_text())[arch]
    load = lambda name: json.loads((RESULT / f"{arch}-{name}.json").read_text())
    roles = load("accounting")["roles"]
    standard = load("execution")
    separate = load("guest-execution")
    targets = dict(standard["targets"])
    tests = dict(standard["tests"])
    for label in guest["targets"]:
        if label in tests:
            raise ValueError(f"{label} executed on both substrates")
        if label in separate["targets"]:
            targets[label] = separate["targets"][label]
        if label in separate["tests"]:
            tests[label] = separate["tests"][label]
    states = {label: sorted({state for _, state in values}) for label, values in targets.items()}
    test_states = {label: row[0] for label, row in tests.items()}
    expected_tests = {label for label, role in roles.items() if role == "test"}
    failed = {k:v for k,v in states.items() if k in roles and v != ["built"]}
    bad_tests = {k:v for k,v in test_states.items() if v != "PASSED"}
    missing = sorted(roles.keys() - states.keys())
    missing_tests = sorted(expected_tests - tests.keys())
    def changes(previous, current):
        return {k:[previous.get(k),current.get(k)] for k in sorted(previous.keys()|current.keys())
                if previous.get(k)!=current.get(k)}
    shard_counts=collections.Counter(row[2] for record in (standard,separate) for row in record["shards"])
    record={"source":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"arch":arch,
            "target_status_counts":dict(collections.Counter(",".join(states.get(label,["MISSING"])) for label in roles)),
            "shards":dict(shard_counts),"failed_targets":failed,"nonpassing_tests":bad_tests,
            "missing":missing,"missing_tests":missing_tests,"previous_missing":old["missing"],
            "previous_missing_tests":old["missing_tests"],
            "role_changes":changes(old["roles"],roles),"target_changes":changes(old["targets"],states),
            "test_changes":changes(old["tests"],test_states),"guest_verified":guest["verified"],
            "invocations":[standard.get("invocation"),separate.get("invocation")],"old_shards":old["shards"]}
    (RESULT / f"{arch}-comparison.json").write_text(json.dumps(record,sort_keys=True,indent=2)+"\n")
    for k in ("target_status_counts","shards","failed_targets","nonpassing_tests","missing","missing_tests","guest_verified"):
        print(arch,k,record[k],flush=True)
    return bool(roles) and not(failed or bad_tests or missing or missing_tests) and guest["verified"]

if __name__=="__main__":
    sys.exit(0 if run(sys.argv[1]) else 1)
