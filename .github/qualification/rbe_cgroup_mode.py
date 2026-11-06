"""Record cgroup hierarchy independently of the test cache setting."""
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
LABELS = [f"//test/rbe_cgroup_mode:{mode}_test" for mode in ("oci", "firecracker")]
work = Path(os.environ["RUNNER_TEMP"]) / "cgroup-worker-mode"
work.mkdir()
report = {"source": SOURCE, "runs": {}}


def save():
    (work / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")


def measure(name, setting):
    result = {"setting": setting, "tests": {}}
    report["runs"][name] = result
    save()
    bep = work / (name + ".bep")
    log = work / (name + ".log")
    with log.open("w") as output:
        p = subprocess.run([
            "bazelisk", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + os.environ["QUALIFY_BAZELRC"],
            "test", "--config=x86_64", "--config=remote-cgroup-worker", "--keep_going", "--build_tests_only",
            "--cache_test_results=yes", "--flaky_test_attempts=1", "--test_arg=-test.v",
            "--test_env=CGROUPV2=" + setting, "--build_event_json_file=" + str(bep), *LABELS,
        ], stdout=output, stderr=subprocess.STDOUT)
    result["exit"] = p.returncode
    logs = log.read_text(errors="replace")
    result["invocations"] = sorted(set(re.findall(r"https://app\.buildbuddy\.io/invocation/[a-f0-9-]{36}", logs)))
    categories = {
        "authentication": r"UNAUTHENTICATED|PERMISSION_DENIED",
        "unavailable": r"UNAVAILABLE|DEADLINE_EXCEEDED",
        "platform_selection": r"no matching (toolchain|execution platform)",
        "package_loading": r"no such package|error loading package",
        "build_failure": r"failed to build|compilation failed",
        "test_failure": r"test failed|tests? FAIL",
    }
    result["diagnostics"] = [kind for kind, pattern in categories.items() if re.search(pattern, logs, re.I)]
    for line in bep.open() if bep.exists() else ():
        event = json.loads(line)
        if "testResult" not in event:
            continue
        label = event["id"]["testResult"]["label"]
        assert label in LABELS and label not in result["tests"]
        data = event["testResult"]
        info = data.get("executionInfo", {})
        testlog = Path("bazel-testlogs") / label[2:].replace(":", "/") / "test.log"
        rows = [json.loads(l.split("CGROUP_MODE ",1)[1]) for l in testlog.read_text().splitlines() if "CGROUP_MODE {" in l] if testlog.exists() else []
        result["tests"][label] = dict(status=data["status"], strategy=info.get("strategy"), cached=data.get("cachedLocally", False) or info.get("cachedRemotely", False), observations=rows)
    save()
    if p.returncode or set(result["tests"]) != set(LABELS):
        raise SystemExit("Remote targets did not finish; inspect the bounded receipt.")
    for label,row in result["tests"].items():
        observation, = row["observations"]
        assert observation["setting"] == setting and observation["arch"] == "amd64"
        assert observation["requested_isolation"] == label.split(":")[1].removesuffix("_test")
        assert row["status"] == "PASSED"


assert subprocess.check_output(["git","rev-parse","HEAD^"],text=True).strip() == SOURCE
for name,setting in (("requested-v2","true"),("other-cache-key","false"),("repeat-v2","true")):
    measure(name,setting)
for label in LABELS:
    first,changed,repeat = [report["runs"][key]["tests"][label] for key in ("requested-v2","other-cache-key","repeat-v2")]
    assert first["strategy"] == changed["strategy"] == "remote"
    assert not first["cached"] and not changed["cached"] and repeat["cached"]
