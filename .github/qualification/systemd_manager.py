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
peers = ("TestSystemdApache", "TestSystemdSSH", "TestSystemdPostgreSQL")
services_digest = "sha256:6da278f8fd241754861a2059396dd7647ff92a5700d2cb67146c556abbbc6326"
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
    services = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/systemd-services_x86_64@" + services_digest
    services_tag = "gvisor.dev/images/systemd-services:latest"
    checked("services-pull", ["docker", "pull", "--platform=linux/amd64", services])
    checked("services-tag", ["docker", "tag", services, services_tag])
    query = checked("services-image", ["docker", "image", "inspect", services_tag])
    info, = json.loads(query.read_bytes())
    assert services in info["RepoDigests"] and (info["Os"], info["Architecture"]) == ("linux", "amd64")
    report["runs"]["services-image"].update(digest=services_digest, image_id=info["Id"], make_tag="d25b4a32a69238ac")
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
    records = []
    measurements = {}
    for line in log.splitlines():
        if begin := re.search(r"USERMGR_BEGIN at_ms=(\d+)$", line):
            measurements = {"begin_ms": int(begin[1]), "journal": {}, "observed_ms": {}, "activation_us": {}, "samples": [], "timeout": []}
            records.append(measurements)
        match = re.search(r"USERMGR_(OBSERVED|ACTIVATION) uid=(100[12]) (?:elapsed_ms|duration_us)=(\d+)$", line)
        if match:
            field = "observed_ms" if match[1] == "OBSERVED" else "activation_us"
            measurements[field][match[2]] = int(match[3])
        journal = re.search(r"USERMGR_JOURNAL uid=(100[12]) read=(true|false) libbpf_warning=(true|false)$", line)
        if journal:
            measurements["journal"][journal[1]] = {"read": journal[2] == "true", "libbpf_warning": journal[3] == "true"}
        match = re.search(r"USERMGR_SAMPLE start_ms=(\d+) elapsed_ms=(\d+) completed=(true|false)$", line)
        if match:
            measurements["samples"].append({"start_ms": int(match[1]), "elapsed_ms": int(match[2]),
                                            "completed": match[3] == "true", "fields": {}, "jobs": [], "user_jobs": []})
        match = re.search(r"USERMGR_FIELD start_ms=(\d+) (MainPID|ControlPID|ActiveState|SubState|Result|ProcessState|WaitChannel|Job|ManagerSocket|UserBusSocket|BasicQueryExit|BasicState|UserJobQueryExit|UserJob)=([\w@,.:\-]{0,160})$", line)
        if match:
            item = next(sample for sample in measurements["samples"] if sample["start_ms"] == int(match[1]))
            if match[2] in {"Job", "UserJob"}:
                item["jobs" if match[2] == "Job" else "user_jobs"].append(match[3].split(","))
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
    report["available_cpus"] = len(os.sched_getaffinity(0))
    report["trials"] = []
    for index, loaded in enumerate((False, True, True, False, False, True), 1):
        trial = {"index": index, "loaded": loaded}
        report["trials"].append(trial)
        run_trial(trial)
        harness.save()
    report["verified"] = all(t["complete"] for t in report["trials"])
    if not report["verified"]:
        raise RuntimeError("incomplete-systemd-comparison")


def run_trial(trial):
    targets = (case, *peers) if trial["loaded"] else (case,)
    selector = "^(" + "|".join(targets) + ")$/^RuntimeDir$"
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
    observations = extract(log)
    trial["observations"] = observations
    peer_times = {}
    for peer, phase, ts in re.findall(r"USERMGR_PEER (TestSystemd[A-Za-z]+) (begin|booted|end) (\d+)", log):
        peer_times.setdefault(peer, {})[phase] = int(ts)
    trial["peers"] = peer_times
    canonical = not trial["cached"] and trial["strategy"] in {"standalone", "local"}
    peer_names = set(targets) - {case}
    peer_ok = set(peer_times) == peer_names and all(
        set(peer_times[p]) == {"begin", "booted", "end"} and trial["xml"].get(p) == "passed" for p in peer_names)
    trial["complete"] = False
    if len(observations) != 1:
        return
    obs, = observations
    trial["overlapping_tests"] = [p for p, t in peer_times.items() if t["begin"] <= obs["begin_ms"] <= t["end"]]
    trial["services_already_booted"] = [p for p in trial["overlapping_tests"] if peer_times[p]["booted"] <= obs["begin_ms"]]
    healthy = code == 0 and all(trial["xml"].get(c) == "passed" for c in (*targets, case + "/RuntimeDir"))
    if healthy:
        observed = (set(obs["activation_us"]) == {"1001", "1002"} and set(obs["journal"]) == {"1001", "1002"}
                    and not obs["timeout"])
    else:
        # A genuine startup failure retains its diagnostic fields even when a peer also failed.
        observed = bool(obs["samples"] and obs["timeout"] and trial["xml"].get(case) == "failed")
    trial["complete"] = canonical and peer_ok and observed


if __name__ == "__main__":
    try:
        main()
    finally:
        report.pop("diagnostics", None)
        harness.save()
