"""Measure the unmodified systemd manager deadline in an isolated Linux guest."""
import json
import os
import platform
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

# The workflow starts Python in isolated mode. Only the pinned donor modules
# staged beside this entry point should supply the shared fixture.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chained_wire as harness


fixture = harness.fixture
work = harness.WORK
source = "6782e66bb04ba41de7da417da53406c2525ac9f1"
carrier = os.environ["GITHUB_SHA"]
digest = "sha256:3682659091e3e184d61555a6840c197002e5f5fe05e1f63443eabe582064b84b"
image = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/systemd-integ_x86_64@" + digest
tag = "gvisor.dev/images/systemd-integ:latest"
runtime = "manager-qual"
target = "//test/e2e:integration_runtime_test"
case = "TestSystemdMultiUser"
repeats = 6
report = {"source": source, "carrier": carrier, "input_image": image, "runs": {}, "inputs": [], "verified": False}
harness.REPORT = report


def save():
    # The donor keeps compiler diagnostics locally. Publish only typed fields.
    summary = {key: value for key, value in report.items() if key != "diagnostics"}
    harness.RECEIPT.write_text(json.dumps(summary, indent=2))


harness.save = save


def checked(name, argv):
    code, path = harness.original_run(name, argv)
    report["runs"][name] = {"exit": code}
    if code:
        # The full process output stays on the worker.
        raise RuntimeError(name)
    return path


def register():
    checked("pull", ["docker", "pull", "--platform=linux/amd64", image])
    checked("tag", ["docker", "tag", image, tag])
    query = checked("image", ["docker", "image", "inspect", tag])
    info, = json.loads(query.read_bytes())
    assert image in info["RepoDigests"] and (info["Os"], info["Architecture"]) == ("linux", "amd64")
    report["runs"]["image"].update(digest=digest, image_id=info["Id"], local_tag=tag, make_tag="7ae0ad9691b7827e")
    executable = work / "old-release" / "runsc"
    logs = work / "runtime-logs"
    logs.mkdir()
    # Same arguments as Make's install_runtime for integration_runtime_test.
    arguments = ["--debug-log", str(logs / "runsc.log.%TEST%.%TIMESTAMP%.%COMMAND%"),
                 "--allow-suid", "--in-sandbox-cgroup=v2", "--TESTONLY-test-name-env=RUNSC_TEST_NAME"]
    name = runtime + "-cgroupv2"
    checked("register", ["sudo", "-n", str(executable), "install", "--config_file=/etc/docker/daemon.json",
                         "--experimental=true", "--runtime=" + name, "--", *arguments])
    config = checked("installed-runtime", ["sudo", "-n", "python3", "-IB", "-c",
                     "import json,sys; print(json.dumps(json.load(open('/etc/docker/daemon.json'))['runtimes'][sys.argv[1]]))", name])
    configured = json.loads(config.read_bytes())
    assert configured.get("path") == str(executable) and configured.get("runtimeArgs") == arguments
    checked("reload", ["sudo", "-n", "systemctl", "reload", "docker"])
    for attempt in range(7):
        info = checked("runtimes", ["docker", "info", "--format", "{{json .Runtimes}}"])
        if name in json.loads(info.read_bytes()):
            break
        time.sleep(1)
    else:
        raise RuntimeError("runtime-registration")
    report["runtime"] = {"name": name, "arguments_match_make": True}


def extract(log):
    records = []
    measurements = {}
    for line in log.splitlines():
        if "USERMGR_BEGIN" in line:
            measurements = {"observed_ms": {}, "activation_us": {}, "samples": [], "timeout": []}
            records.append(measurements)
        match = re.search(r"USERMGR_(OBSERVED|ACTIVATION) uid=(100[12]) (?:elapsed_ms|duration_us)=(\d+)$", line)
        if match:
            field = "observed_ms" if match[1] == "OBSERVED" else "activation_us"
            measurements[field][match[2]] = int(match[3])
        match = re.search(r"USERMGR_SAMPLE start_ms=(\d+) elapsed_ms=(\d+) completed=(true|false)$", line)
        if match:
            measurements["samples"].append({"start_ms": int(match[1]), "elapsed_ms": int(match[2]),
                                            "completed": match[3] == "true", "fields": {}, "jobs": []})
        match = re.search(r"USERMGR_FIELD start_ms=(\d+) (MainPID|ControlPID|ActiveState|SubState|Result|ProcessState|WaitChannel|Job)=([\w@,.:\-]{0,160})$", line)
        if match:
            item = next(sample for sample in measurements["samples"] if sample["start_ms"] == int(match[1]))
            if match[2] == "Job":
                item["jobs"].append(match[3].split(","))
            else:
                item["fields"][match[2]] = match[3]
        match = re.search(r"user@(100[12])\.service did not become active", line)
        if match and measurements:
            measurements["timeout"].append(match[1])
    return records


def main():
    assert platform.machine() == "x86_64" and fixture.git("rev-parse", "HEAD^") == source
    fixture.FLAGS[fixture.FLAGS.index("--config=aarch64")] = "--config=x86_64"
    fixture.run = harness.run
    harness.admit(row for row in harness.INPUTS if row[0] in ("protobuf", "bazel_features", "rules_android", "bazel_skylib"))
    fixture.select(source)
    fixture.stage_old(report["runs"].setdefault("runtime-build", {}))
    register()
    fixture.select(carrier)
    selector = "^TestSystemdMultiUser$/^RuntimeDir$"
    bep = work / "systemd.bep"
    code, _ = harness.run("systemd", [*fixture.BAZEL, "test", *fixture.FLAGS, "--strategy=TestRunner=standalone",
                                       "--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                                       "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=RUNTIME=" + runtime,
                                       "--test_timeout=1800", "--test_filter=" + selector, "--test_arg=-test.run=" + selector,
                                       "--test_arg=-test.count=" + str(repeats), "--test_output=errors",
                                       "--build_event_json_file=" + str(bep), target])
    test = report["runs"]["systemd"] = {"exit": code}
    results = [event["testResult"] for line in bep.read_text().splitlines() if (event := json.loads(line)).get("id", {}).get("testResult", {}).get("label") == target and "testResult" in event]
    if len(results) != 1:
        raise RuntimeError("missing-test-result")
    result, = results
    files = fixture.output_files(target, result)
    log = files["test.log"].read_text(errors="replace")
    test.update(strategy=result["executionInfo"]["strategy"], status=result["status"], cached=result.get("cachedLocally",False))
    test["file_sha256"] = {name: fixture.H["digest"](path) for name, path in files.items()}
    test["xml"] = [{"name": el.get("name"), "skipped": el.find("skipped") is not None,
                    "failed": el.find("failure") is not None or el.find("error") is not None}
                   for el in ET.parse(files["test.xml"]).iter("testcase")]
    runs = test["observations"] = extract(log)
    succeeded = test["completed_controls"] = len(re.findall(r"--- PASS: TestSystemdMultiUser/RuntimeDir \(", log))
    failed = test["activation_timeouts"] = sum(bool(row["timeout"]) for row in runs)
    # An actual timeout is evidence. Other setup or runtime failures require diagnosis.
    for note in ("docker run failed", "systemd did not boot", "missing mount", "permission denied", "manager did not reach the running state"):
        if note in log:
            test.setdefault("setup_conditions", []).append(note)
    report["verified"] = (not test["cached"] and test["strategy"] in {"standalone","local"}
                          and len(runs) == repeats and succeeded+failed == repeats
                          and all(set(row["observed_ms"]) == {"1001","1002"} or row["timeout"] for row in runs)
                          and all(row["samples"] for row in runs if row["timeout"])
                          and bool(test["xml"]) and not any(row["skipped"] for row in test["xml"]))
    if not report["verified"]:
        raise RuntimeError("incomplete-manager-observations")


if __name__ == "__main__":
    try:
        main()
    finally:
        report.pop("diagnostics", None)
        harness.save()
