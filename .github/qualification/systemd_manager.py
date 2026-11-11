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
    version = checked("manager-version", ["docker", "run", "--rm", "--entrypoint=/usr/bin/systemctl", image, "--version"])
    line = next(line for line in version.read_text().splitlines() if line.startswith("systemd "))
    report["runs"]["manager-version"]["version"] = line[:100]
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
    snapshots = {}
    timeouts = re.findall(r"BOTH_TIMEOUT unit=user@(100[12])\.service", log)
    for line in log.splitlines():
        match = re.search(r"BOTH_SAMPLE phase=([a-z-]+) uid=(100[12]) complete=(true|false) elapsed_ms=(\d+)$", line)
        if match:
            phase, uid, done, elapsed = match.groups()
            snapshots.setdefault(phase, {})[uid] = {"complete": done == "true", "elapsed_ms": int(elapsed), "fields": {}, "observations": {}}
        match = re.search(r"BOTH_FIELD phase=([a-z-]+) uid=(100[12]) (\w+)=([\w@,.:\-]{0,420})$", line)
        if match:
            phase, uid, key, value = match.groups()
            sample = snapshots[phase][uid]
            if key in {"Process", "UserJob", "SystemJob", "UserJournal", "UnitJournal", "UserError"}:
                sample["observations"].setdefault(key, []).append(value.split(","))
            else:
                sample["fields"][key] = value
    return list(dict.fromkeys(timeouts)), snapshots


def main():
    assert platform.machine() == "x86_64" and fixture.git("rev-parse", "HEAD^") == source
    fixture.FLAGS[fixture.FLAGS.index("--config=aarch64")] = "--config=x86_64"
    fixture.run = harness.run
    harness.admit(row for row in harness.INPUTS if row[0] in ("protobuf", "bazel_features", "rules_android", "bazel_skylib"))
    fixture.select(source)
    fixture.stage_old(report["runs"].setdefault("runtime-build", {}))
    register()
    fixture.select(carrier)
    report["available_cpus"] = len(os.sched_getaffinity(0))
    report["trials"] = []
    deadline = time.monotonic() + 22 * 60
    for index in range(1, 17):
        trial = {"index": index}
        report["trials"].append(trial)
        run_trial(trial)
        harness.save()
        captured = [t for t in report["trials"] if t["complete"]]
        if (sum(bool(t["timeout"]) for t in captured) >= 2 and any(not t["timeout"] for t in captured)) or time.monotonic() > deadline:
            break
    report["verified"] = all(t["complete"] for t in report["trials"])
    if not report["verified"]:
        raise RuntimeError("incomplete-systemd-comparison")


def run_trial(trial):
    selector = "^" + case + "$/^RuntimeDir$"
    name = "systemd-" + str(trial["index"])
    bep = work / (name + ".bep")
    code, _ = harness.run(name, [*fixture.BAZEL, "test", *fixture.FLAGS, "--strategy=TestRunner=standalone",
                                "--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                                "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=RUNTIME=" + runtime,
                                "--test_timeout=360", "--test_filter=" + selector, "--test_arg=-test.run=" + selector,
                                "--test_arg=-test.parallel=4", "--test_output=errors",
                                "--build_event_json_file=" + str(bep), target])
    trial["exit"] = code
    result, = (event["testResult"] for line in bep.read_text().splitlines()
               if (event := json.loads(line)).get("id", {}).get("testResult", {}).get("label") == target and "testResult" in event)
    files = fixture.output_files(target, result)
    log = files["test.log"].read_text(errors="replace")
    trial.update(strategy=result["executionInfo"]["strategy"], status=result["status"], cached=result.get("cachedLocally", False))
    trial["file_sha256"] = {path: fixture.H["digest"](file) for path, file in files.items()}
    trial["xml"] = {e.get("name"): ("skipped" if e.find("skipped") is not None else "failed"
                    if e.find("failure") is not None or e.find("error") is not None else "passed")
                    for e in ET.parse(files["test.xml"]).iter("testcase")}
    trial["xml_seconds"] = {e.get("name"): e.get("time") for e in ET.parse(files["test.xml"]).iter("testcase")}
    trial["timeout"], trial["snapshots"] = extract(log)
    canonical = not trial["cached"] and trial["strategy"] in {"standalone", "local"}
    if code == 0:
        result_ok = (trial["status"] == "PASSED" and not trial["timeout"] and
                     all(trial["xml"].get(name) == "passed" for name in (case, case + "/RuntimeDir")))
        phases = {"ready"}
    else:
        result_ok = (code == 3 and trial["status"] == "FAILED" and len(trial["timeout"]) == 1 and
                     trial["xml"].get(case) == "failed" and
                     "user@" + trial["timeout"][0] + ".service did not become active" in log)
        phases = {"timeout", "after-timeout"}
    complete = phases == set(trial["snapshots"])
    for users in trial["snapshots"].values():
        complete &= set(users) == {"1001", "1002"}
        for sample in users.values():
            fields = sample["fields"]
            complete &= (sample["complete"] and {"ActiveState", "SubState", "MainPID", "ManagerSocket", "BusSocket",
                         "BasicExit", "UserJobsExit", "UnitJournalExit", "UserJournalExit"}.issubset(fields))
    trial["complete"] = canonical and result_ok and complete


if __name__ == "__main__":
    try:
        main()
    finally:
        report.pop("diagnostics", None)
        harness.save()
