"""Measure real CUBIC startup under one ordered, transient ACK delay."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
ROOT = "TestVirtualTCPHyStartTransientDelay"
CASES = {f"{ROOT}/control", f"{ROOT}/pulse"}


def measure_startup(samples: dict) -> dict:
    """Check the wire input independently of which detector fires, if any."""
    result = {}
    prefixes = {}
    for name, delay in (("control", 0), ("pulse", 25_000_000)):
        sample = samples[f"{ROOT}/{name}"]
        startup = sample["Startup"]
        mss, first = startup["MSS"], startup["DataSequence"]
        deadline = startup["Deadline"]
        if (sample["Algorithm"] != "cubic" or startup["DelayNS"] != delay or mss != 1420
                or sample["Bytes"] != 16 << 20 or sample["Retransmits"] or sample["RTOs"]
                or deadline["MSS"] != mss or deadline["AtNS"] != startup["FirstPayloadNS"] + 1_000_000_000
                or deadline["AppRemainingBytes"] <= 0 or deadline["Probe"]["BufferedBytes"] <= 0
                or deadline["Probe"]["SndWnd"] <= deadline["Cwnd"] * mss):
            raise ValueError(f"{name}: workload, deadline, demand or advertised window differs: {deadline}")
        wire = [p for p in sample["Trace"] if p["From"] == 1 and p["Flags"] == 16
                and 0 < (p["Acknowledgment"] - first) % 2**32 <= 160 * mss]
        early, pulse_rtt, after_rtt = [], [], []
        previous_offset = previous_at = 0
        for packet, ack in zip(wire, startup["ACKs"], strict=True):
            offset = (packet["Acknowledgment"] - first) % 2**32
            affected = 30 * mss < offset <= 70 * mss
            expected = max(previous_at, packet["NominalAtNS"] + (delay if affected else 0))
            if (offset <= previous_offset or packet["AtNS"] != expected or ack["AtNS"] != expected
                    or ack["SndUna"] != packet["Acknowledgment"] or not packet.get("TSEcr")):
                raise ValueError(f"{name}: scheduled ACK or sender identity differs at byte {offset}")
            previous_offset, previous_at = offset, expected
            rtt = (ack["TSNow"] - packet["TSEcr"]) % 2**32
            if affected:
                pulse_rtt.append(rtt)
            elif offset > 70 * mss:
                after_rtt.append(rtt)
            else:
                early.append((packet, ack))
        if not early or not pulse_rtt or not after_rtt:
            raise ValueError(f"{name}: incomplete ACK history across the selected flight")
        prefixes[name] = (first, mss, startup["FirstPayloadNS"], sample["PayloadSHA256"], early)
        initial = startup["ACKs"][0]["Ssthresh"]
        exit_ack = next((a for a in startup["ACKs"] if a["Ssthresh"] != initial), None)
        result[name] = {
            "first_exit_in_trace": exit_ack,
            "affected_ack_count": len(pulse_rtt), "affected_min_rtt_ms": min(pulse_rtt),
            "after_ack_count": len(after_rtt), "after_min_rtt_ms": min(after_rtt),
            "deadline": deadline, "transfer_finished_ns": sample["VirtualNS"],
        }
    if prefixes["control"] != prefixes["pulse"]:
        raise ValueError("The trials differ before the first pulse ACK or in the delivered payload")
    return result


def native_results(directory: Path, phase: str, owner: str, runs: int) -> tuple[dict, dict]:
    manifest = json.loads((directory / "events.jsonl.manifest.json").read_text())
    event_body = (directory / "events.jsonl").read_bytes()
    if (len(event_body) != manifest["events"]["bytes"]
            or hashlib.sha256(event_body).hexdigest() != manifest["events"]["sha256"]):
        raise ValueError(f"{phase}: native event identity mismatch")
    native = {}
    for event in map(json.loads, event_body.splitlines()):
        if "testResult" in event["id"]:
            if event["id"]["testResult"]["label"] != owner:
                raise ValueError(f"{phase}: unexpected native test owner")
            key = json.dumps(event["id"]["testResult"], sort_keys=True)
            value = event["testResult"]
            if (key in native or value["status"] != "PASSED" or value.get("cachedLocally")
                    or value.get("executionInfo", {}).get("cachedRemotely")
                    or value.get("executionInfo", {}).get("strategy") != "remote"):
                raise ValueError(f"{phase}: native attempt identity, freshness or execution strategy differs")
            native[key] = value
    if len(native) != runs:
        raise ValueError(f"{phase}: native attempt census differs")
    return manifest, native


def observations(evidence: Path, phase: str, runs: int) -> dict:
    directory = evidence / ("invocation-" + phase)
    verdict = json.loads((directory / "verdict.json").read_text())
    executed = verdict["executed"]
    if verdict["status"] != "PASS" or len(executed) != runs:
        raise ValueError(f"{phase}: expected {runs} passing native attempts")
    if {row["run"] for row in executed} != set(range(1, runs + 1)):
        raise ValueError(f"{phase}: incomplete run census")
    for row in executed:
        if (row["owner"]["label"] != OWNER or row["status"] != "PASSED"
                or row["cached_locally"] or row["attempt"] != 1 or row["shard"] != 1):
            raise ValueError(f"{phase}: unexpected attempt identity: {row}")
    manifest, native = native_results(directory, phase, OWNER, runs)
    samples = {}
    xml_runs = set()
    references = []
    paths = set()
    executed_keys = {(row["owner"]["label"], row["owner"]["configuration"], row["run"],
                      row["attempt"], row["shard"]) for row in executed}
    for row in manifest["reports"]:
        identity = row["identity"]
        key = (identity["label"], identity["configuration"]["id"], identity["run"],
               identity["attempt"], identity["shard"])
        if key not in executed_keys:
            raise ValueError(f"{phase}: report does not match an executed attempt")
        outputs = native[json.dumps(identity, sort_keys=True)]["testActionOutput"]
        matches = [item for item in outputs if item["name"] == row["name"]]
        if len(matches) != 1 or not matches[0]["uri"].endswith("/" + row["path"].removeprefix("reports/")):
            raise ValueError(f"{phase}: report has no unique native output")
        path = directory / row["path"]
        if path in paths:
            raise ValueError(f"{phase}: attempts share a retained report path")
        paths.add(path)
        body = path.read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"{phase}: report hash mismatch")
        run = identity["run"]
        if row["name"] == "test.xml":
            cases = ET.fromstring(body).findall(".//testcase")
            names = [case.attrib["name"] for case in cases]
            if (set(names) != CASES | {ROOT} or len(names) != len(CASES) + 1
                    or any(list(case) for case in cases) or run in xml_runs):
                raise ValueError(f"{phase}: unexpected XML cases {names}")
            xml_runs.add(run)
        elif row["name"] == "test.log":
            values = [json.loads(line.split("VIRTUAL_TCP ", 1)[1])
                      for line in body.decode().splitlines() if "VIRTUAL_TCP " in line]
            if {value["Case"] for value in values} != CASES or len(values) != len(CASES) or run in samples:
                raise ValueError(f"{phase}: missing or duplicate virtual transfers")
            samples[run] = {value["Case"]: value for value in values}
        else:
            raise ValueError(f"{phase}: unexpected report {row['name']}")
        references.append(row)
    if set(samples) != set(range(1, runs + 1)) or xml_runs != set(samples):
        raise ValueError(f"{phase}: log/XML census differs")
    baseline = samples[1]
    for run, sample in samples.items():
        if sample != baseline:
            raise ValueError(f"{phase}: virtual packet observations differ in run {run}")
    return {"attempts": runs, "reference_sample": baseline, "reports": references,
            "startup": measure_startup(baseline)}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owners = [OWNER, OWNER + "_nogo"]
    if config["targets"] != owners:
        raise ValueError("Unexpected virtual TCP owners")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; '
                           'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
    formatted = invoke("gofmt", ["run", *options, "--run_under=" + formatter,
                                "@io_bazel_rules_go//go:go"], "command", targets=[])
    if formatted["status"] != "PASS":
        raise RuntimeError("Canonical Go formatting failed")
    selected = ["--strategy=TestRunner=remote", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=all", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1"]
    result = {"status": "INCOMPLETE", "source": config["source"], "phases": {}}
    try:
        for phase, count, extra in (("virtual-normal", 2, []), ("virtual-race", 1, ["--config=race"])):
            checked = invoke(phase, ["test", *options, *extra, *selected,
                                     f"--runs_per_test={count}", f"--test_arg=-test.run=^{ROOT}$", OWNER], "test", targets=[OWNER])
            if checked["status"] != "PASS":
                raise RuntimeError(f"{phase}: native test did not pass")
            result["phases"][phase] = observations(evidence, phase, count)
        if (result["phases"]["virtual-normal"]["reference_sample"]
                != result["phases"]["virtual-race"]["reference_sample"]):
            raise ValueError("Normal and race virtual packet observations differ")
        analyzed = invoke("virtual-nogo", ["test", *options, *selected, "--runs_per_test=1",
                                           OWNER + "_nogo"], "test", targets=[OWNER + "_nogo"])
        if analyzed["status"] != "PASS":
            raise RuntimeError("Both-architecture analyzer owner did not pass")
        native_results(evidence / "invocation-virtual-nogo", "virtual-nogo", OWNER + "_nogo", 1)
        result["status"] = "PASS"
    except (OSError, ValueError, RuntimeError) as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        (evidence / "virtual-tcp-determinism.json").write_text(json.dumps(result, indent=2) + "\n")
