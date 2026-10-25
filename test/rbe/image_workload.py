"""Compare classic and RFC 9406 startup on the same corrected CUBIC sender."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
UNIT = "//pkg/tcpip/transport/tcp:tcp_test"
ROOT = "TestVirtualTCPHyStartComparison"
CASES = {f"{ROOT}/{scheme}_{path}" for scheme in ("classic", "plus")
         for path in ("control", "pulse", "shifted_pulse", "fifo")}
UNIT_ROOT = "TestCubicHyStartPlus"
UNIT_CASES = {f"{UNIT_ROOT}/{case}" for case in ("byte_credit", "relief", "persistent", "loss", "timeout")}


def measure_startup(samples: dict, scheme: str) -> dict:
    """Check the wire input independently of which detector fires, if any."""
    result = {}
    prefixes = {}
    for name, delay, lower in (("control", 0, 30), ("pulse", 25_000_000, 30), ("shifted_pulse", 25_000_000, 28)):
        sample = samples[f"{ROOT}/{scheme}_{name}"]
        startup = sample["Startup"]
        mss, first = startup["MSS"], startup["DataSequence"]
        deadline = startup["Deadline"]
        if (sample["Algorithm"] != "cubic" or startup["DelayNS"] != delay or startup["LowerMSS"] != lower or mss != 1420
                or sample["RateBytesPerSecond"] != 12_500_000 or sample["OneWayDelayNS"] != 50_000_000
                or sample.get("ForwardQueue") or not startup["Transitions"]
                or any(ack["Plus"] != (scheme == "plus") for ack in startup["ACKs"])
                or sample["Bytes"] != 16 << 20 or sample["Retransmits"] or sample["RTOs"]
                or deadline["MSS"] != mss or deadline["AtNS"] != startup["FirstPayloadNS"] + 1_000_000_000
                or deadline["AppRemainingBytes"] <= 0 or deadline["Probe"]["BufferedBytes"] <= 0):
            raise ValueError(f"{name}: workload, deadline, demand or advertised window differs: {deadline}")
        wire = [p for p in sample["Trace"] if p["From"] == 1 and p["Flags"] == 16
                and 0 < (p["Acknowledgment"] - first) % 2**32 <= 160 * mss]
        early, pulse_rtt, after_rtt, missing_echo = [], [], [], []
        spill, after_echo = [], []
        selected_count = after_count = 0
        previous_offset = previous_at = 0
        for packet, ack in zip(wire, startup["ACKs"], strict=True):
            offset = (packet["Acknowledgment"] - first) % 2**32
            affected = lower * mss < offset <= (lower + 40) * mss
            expected = max(previous_at, packet["NominalAtNS"] + (delay if affected else 0))
            if (offset <= previous_offset or packet["AtNS"] != expected or ack["AtNS"] != expected
                    or ack["SndUna"] != packet["Acknowledgment"]):
                raise ValueError(f"{name}: scheduled ACK or sender identity differs at byte {offset}")
            previous_offset, previous_at = offset, expected
            echo = packet.get("TSEcr")
            if not echo:
                missing_echo.append(offset)
            if affected:
                selected_count += 1
                if echo:
                    pulse_rtt.append((ack["TSNow"] - echo) % 2**32)
            elif offset > (lower + 40) * mss:
                after_count += 1
                if packet["AtNS"] > packet["NominalAtNS"]:
                    spill.append({"ack_bytes": offset, "extra_ns": packet["AtNS"] - packet["NominalAtNS"]})
                if echo:
                    after_rtt.append((ack["TSNow"] - echo) % 2**32)
                    after_echo.append((offset, after_rtt[-1]))
            else:
                early.append((packet, ack))
        if not early or not selected_count or not after_count:
            raise ValueError(f"{name}: incomplete ACK history across the selected flight")
        prefixes[name] = (first, mss, startup["FirstPayloadNS"], sample["PayloadSHA256"], early)
        initial = startup["ACKs"][0]["Ssthresh"]
        phase_changes = [event for event in startup["Transitions"] if
                         event["Before"]["CSS"] != event["After"]["CSS"] or
                         event["Before"]["Ssthresh"] != event["After"]["Ssthresh"]]
        exit_ack = next((e["After"] for e in phase_changes if e["After"]["Ssthresh"] != initial), None)
        result[name] = {
            "first_terminal_exit": exit_ack, "phase_changes": phase_changes,
            "affected_ack_count": selected_count, "affected_min_rtt_ms": min(pulse_rtt, default=None),
            "after_ack_count": after_count, "after_min_rtt_ms": min(after_rtt, default=None),
            "missing_or_zero_echo_offsets": missing_echo,
            "unselected_ack_fifo_delay": spill,
            "first_after_with_echo_at_most_101ms": next((row for row in after_echo if row[1] <= 101), None),
            "deadline": deadline, "rwnd_headroom_bytes": deadline["Probe"]["SndWnd"] - deadline["Cwnd"] * mss,
            "transfer_finished_ns": sample["VirtualNS"],
        }
    for name in ("pulse", "shifted_pulse"):
        if (prefixes[name][:-1] != prefixes["control"][:-1]
                or prefixes[name][-1] != prefixes["control"][-1][:len(prefixes[name][-1])]):
            raise ValueError(f"{name}: trials differ before the first pulse ACK or in the delivered payload")
    return result


def measure_fifo(sample: dict, scheme: str) -> dict:
    startup, queue = sample["Startup"], sample["ForwardQueue"]
    mark = startup["Deadline"]
    if (sample["Algorithm"] != "cubic" or sample["RecoveryClass"] != "finite_fifo"
            or sample["Bytes"] != 16 << 20 or mark["MSS"] != 1420
            or mark["AtNS"] != startup["FirstPayloadNS"] + 1_000_000_000
            or mark["AppRemainingBytes"] <= 0 or mark["Probe"]["BufferedBytes"] <= 0
            or sample["RateBytesPerSecond"] != queue["Rate"] or queue["Rate"] != 1_250_000
            or sample["OneWayDelayNS"] != 50_000_000 or sample["BDPBytes"] != queue["Capacity"]
            or queue["Capacity"] != 125_000 or startup["DelayNS"] or not startup["Transitions"]
            or any(ack["Plus"] != (scheme == "plus") for ack in startup["ACKs"])
            or queue["AdmittedPackets"] + queue["DroppedPackets"] != queue["OfferedPackets"]
            or queue["AdmittedBytes"] + queue["DroppedBytes"] != queue["OfferedBytes"]
            or not 0 < queue["FirstSecondPeakNS"] <= queue["PeakBacklogNS"] <= 100_000_000
            or not 0 <= queue["FirstSecondAbove25msNS"] <= min(1_000_000_000, queue["Above25msNS"])
            or not 0 <= queue["LongestAbove25msNS"] <= queue["Above25msNS"] <= sample["VirtualNS"]):
        raise ValueError(f"{scheme}: FIFO exposure, accounting or demand differs: {queue}")
    return {"queue": queue, "deadline": mark, "transfer_finished_ns": sample["VirtualNS"],
            "peak_cwnd": startup["PeakCwnd"], "first_second_peak_cwnd": startup["FirstSecondPeakCwnd"],
            "peak_flight_bytes": sample["MaxOutstandingBeforeBurst"],
            "sustained_above_25ms_for_one_base_rtt": queue["LongestAbove25msNS"] >= 100_000_000}


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


def observations(evidence: Path, phase: str, runs: int, owner: str = OWNER) -> dict:
    root, leaves = (ROOT, CASES) if owner == OWNER else (UNIT_ROOT, UNIT_CASES)
    directory = evidence / ("invocation-" + phase)
    verdict = json.loads((directory / "verdict.json").read_text())
    executed = verdict["executed"]
    if verdict["status"] != "PASS" or len(executed) != runs:
        raise ValueError(f"{phase}: expected {runs} passing native attempts")
    if {row["run"] for row in executed} != set(range(1, runs + 1)):
        raise ValueError(f"{phase}: incomplete run census")
    for row in executed:
        if (row["owner"]["label"] != owner or row["status"] != "PASSED"
                or row["cached_locally"] or row["attempt"] != 1 or row["shard"] != 1):
            raise ValueError(f"{phase}: unexpected attempt identity: {row}")
    manifest, native = native_results(directory, phase, owner, runs)
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
            if (set(names) != leaves | {root} or len(names) != len(leaves) + 1
                    or any(list(case) for case in cases) or run in xml_runs):
                raise ValueError(f"{phase}: unexpected XML cases {names}")
            xml_runs.add(run)
        elif row["name"] == "test.log":
            values = [json.loads(line.split("VIRTUAL_TCP ", 1)[1])
                      for line in body.decode().splitlines() if "VIRTUAL_TCP " in line]
            expected = leaves if owner == OWNER else set()
            if {value["Case"] for value in values} != expected or len(values) != len(expected) or run in samples:
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
    result = {"attempts": runs, "reference_sample": baseline, "reports": references}
    if owner == OWNER:
        result["startup"] = {scheme: measure_startup(baseline, scheme) for scheme in ("classic", "plus")}
        result["fifo"] = {scheme: measure_fifo(baseline[f"{ROOT}/{scheme}_fifo"], scheme) for scheme in ("classic", "plus")}
        if len({sample["PayloadSHA256"] for sample in baseline.values()}) != 1:
            raise ValueError("The eight arms did not deliver the same payload")
    return result


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owners = [OWNER, OWNER + "_nogo", UNIT, UNIT + "_nogo"]
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
        unit = invoke("startup-unit", ["test", *options, *selected, "--runs_per_test=1",
                                      f"--test_arg=-test.run=^{UNIT_ROOT}$", UNIT], "test", targets=[UNIT])
        if unit["status"] != "PASS":
            raise RuntimeError("Slow-start unit owner did not pass")
        result["phases"]["startup-unit"] = observations(evidence, "startup-unit", 1, UNIT)
        for phase, count, extra in (("virtual-normal", 2, []), ("virtual-race", 1, ["--config=race"])):
            checked = invoke(phase, ["test", *options, *extra, *selected,
                                     f"--runs_per_test={count}", f"--test_arg=-test.run=^{ROOT}$", OWNER], "test", targets=[OWNER])
            if checked["status"] != "PASS":
                raise RuntimeError(f"{phase}: native test did not pass")
            result["phases"][phase] = observations(evidence, phase, count)
        if (result["phases"]["virtual-normal"]["reference_sample"]
                != result["phases"]["virtual-race"]["reference_sample"]):
            raise ValueError("Normal and race virtual packet observations differ")
        for phase, analyzer in (("virtual-nogo", OWNER + "_nogo"), ("startup-nogo", UNIT + "_nogo")):
            analyzed = invoke(phase, ["test", *options, *selected, "--runs_per_test=1",
                                      analyzer], "test", targets=[analyzer])
            if analyzed["status"] != "PASS":
                raise RuntimeError(f"{phase}: analyzer owner did not pass")
            native_results(evidence / f"invocation-{phase}", phase, analyzer, 1)
        result["status"] = "PASS"
    except (OSError, ValueError, RuntimeError) as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        (evidence / "virtual-tcp-determinism.json").write_text(json.dumps(result, indent=2) + "\n")
