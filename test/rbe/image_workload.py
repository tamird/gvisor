"""Compare sustained single-flow Reno/CUBIC over one finite-buffer path."""
from collections import deque
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report

OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
SDK = "@io_bazel_rules_go//go:go"
PYTHON = "@rules_python//python/bin:python"
ROOT = "TestVirtualTCPSustainedFIFO"
CASES = {ROOT + "/" + algorithm for algorithm in ("reno", "cubic")}
PARENTS = {ROOT}


def observations(evidence: Path, phase: str, runs: int, cases: set[str] = CASES,
                 parents: set[str] = PARENTS) -> dict:
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
    manifest = json.loads((directory / "events.jsonl.manifest.json").read_text())
    event_body = (directory / "events.jsonl").read_bytes()
    if (len(event_body) != manifest["events"]["bytes"]
            or hashlib.sha256(event_body).hexdigest() != manifest["events"]["sha256"]):
        raise ValueError(f"{phase}: native event identity mismatch")
    events = [json.loads(line) for line in event_body.splitlines()]
    native = {json.dumps(event["id"]["testResult"], sort_keys=True): event["testResult"]
              for event in events if "testResult" in event.get("id", {})}
    if len(native) != runs:
        raise ValueError(f"{phase}: unexpected native attempt count")
    for value in native.values():
        if (value["status"] != "PASSED" or value.get("cachedLocally", False)
                or value.get("executionInfo", {}).get("cachedRemotely", False)
                or value.get("executionInfo", {}).get("strategy") != "local"):
            raise ValueError(f"{phase}: native attempt was not fresh and local")
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
        native_row = native.get(json.dumps(identity, sort_keys=True))
        if native_row is None:
            raise ValueError(f"{phase}: report has no native attempt")
        native_outputs = [item for item in native_row["testActionOutput"] if item["name"] == row["name"]]
        if (len(native_outputs) != 1 or not native_outputs[0]["uri"].endswith(
                "/" + row["path"].removeprefix("reports/"))):
            raise ValueError(f"{phase}: native report path differs")
        path = directory / row["path"]
        if path in paths:
            raise ValueError(f"{phase}: attempts share a retained report path")
        paths.add(path)
        body = path.read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"{phase}: report hash mismatch")
        run = identity["run"]
        if row["name"] == "test.xml":
            xml_cases = ET.fromstring(body).findall(".//testcase")
            names = [case.attrib["name"] for case in xml_cases]
            if (set(names) != cases | parents or len(names) != len(cases | parents)
                    or any(list(case) for case in xml_cases) or run in xml_runs):
                raise ValueError(f"{phase}: unexpected XML cases {names}")
            xml_runs.add(run)
        elif row["name"] == "test.log":
            values = [json.loads(line.split("VIRTUAL_FIFO ", 1)[1])
                      for line in body.decode().splitlines() if "VIRTUAL_FIFO " in line]
            if {value["Case"] for value in values} != cases or len(values) != len(cases) or run in samples:
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
    return {"attempts": runs, "reference_sample": baseline, "reports": references}


def check_observation(value: dict) -> dict:
    """Replay finite admission/service and independently reduce the fixed window."""
    algorithm = value["Case"].removeprefix(ROOT + "/")
    assert algorithm in ("reno", "cubic") and value["Case"] in CASES
    expected = {"Algorithms": [algorithm], "Order": [0], "StaggerNS": 0,
                "RateBytesPerSecond": 1250000, "OneWayDelayNS": 50000000,
                "CapacityBytes": 31250, "WindowStartNS": 5 * 10**9, "WindowEndNS": 20 * 10**9}
    assert {key: value[key] for key in expected} == expected
    assert "ApplicationOrder" not in value and "Diagnostic" not in value
    start, end, finish = value["WindowStartNS"], value["WindowEndNS"], value["VirtualEndNS"]
    assert end <= finish <= 60 * 10**9 and len(value["Flows"]) == 1
    flow = value["Flows"][0]
    accepted = flow["Accepted"]
    assert (flow["Algorithm"], flow["LocalPort"], flow["RemotePort"]) == (algorithm, 40000, 8080)
    assert 0 < accepted < 64 << 20 and flow["Delivered"] == flow["Acknowledged"] == accepted
    assert flow["EOF"] is True and flow["FinWait2"] is True
    assert 0 < flow["ConnectedNS"] <= flow["StartedNS"] < start
    period = bytes((offset * 17 + offset // 251) % 256 for offset in range(251 * 256))
    payload_bytes = (period * ((accepted + len(period) - 1) // len(period)))[:accepted]
    assert hashlib.sha256(payload_bytes).hexdigest() == flow["PayloadSHA256"]
    observed = value["Sustained"]
    assert observed["OfferedBytes"] == 64 << 20
    events, flags = value["Events"], observed["PacketFlags"]
    assert 0 < len(events) <= 50000 and len(flags) == len(events)
    next_tx = [0, 0]
    queued = deque()
    occupancy = highest = peak = busy = 0
    service_payload = service_retx = Fraction(0)
    queue_changes = []
    drops, retransmissions = [], []
    previous_capture = -1
    bins = [{"start_ns": at, "end_ns": at + 10**9, "original_drops": 0, "retransmission_drops": 0,
             "retransmissions": 0, "new_sequence_bytes": 0} for at in range(start, end, 10**9)]
    ack_emitted = ack_arrived = data_arrived = 0
    for ordinal, (row, tcp_flags) in enumerate(zip(events, flags)):
        match row:
            case [number, capture, direction, index, wire, payload, sequence, ack,
                  service_start, service_end, arrival, dropped, retx] if all(type(x) is int for x in row):
                pass
            case _:
                raise ValueError(f"{algorithm}: malformed event {ordinal}")
        assert number == ordinal and index == 0 and direction in (0, 1)
        assert previous_capture <= capture <= finish and 40 <= wire <= 1500 and 0 <= payload <= wire - 40
        assert 0 <= sequence < 2**32 and 0 <= ack < 2**32 and dropped in (0, 1) and retx in (0, 1)
        assert type(tcp_flags) is int and 0 <= tcp_flags <= 255
        previous_capture = capture
        while queued and queued[0][0] <= capture:
            occupancy -= queued.popleft()[1]
        assert dropped == int(direction == 0 and occupancy + wire > 31250), (algorithm, ordinal, "admission")
        added = 0
        if direction == 0 and payload:
            offset = (sequence - flow["DataSequence"]) % 2**32
            assert offset + payload <= accepted and retx == int(offset < highest)
            added = max(highest, offset + payload) - highest
            highest += added
        else:
            assert not retx and not payload
        if dropped:
            drops.append({"event": ordinal, "at_ns": capture, "bytes": payload, "retransmission": bool(retx)})
        if retx:
            retransmissions.append({"event": ordinal, "at_ns": capture, "bytes": payload, "dropped": bool(dropped)})
        if start < capture <= end:
            second = min((capture - start - 1) // 10**9, len(bins) - 1)
            bucket = bins[second]
            bucket["original_drops"] += int(dropped and not retx)
            bucket["retransmission_drops"] += int(dropped and retx)
            bucket["retransmissions"] += retx
            bucket["new_sequence_bytes"] += added
            ack_emitted += int(direction == 1 and tcp_flags & 16 != 0 and tcp_flags & 3 == 0)
        if dropped:
            assert (service_start, service_end, arrival) == (-1, -1, -1)
            continue
        wanted_start = max(next_tx[direction], capture)
        wanted_end = wanted_start + (wire * 10**9 + 1250000 - 1) // 1250000
        assert (service_start, service_end, arrival) == (wanted_start, wanted_end, wanted_end + 50000000)
        assert arrival <= finish
        next_tx[direction] = service_end
        if start < arrival <= end:
            ack_arrived += int(direction == 1 and tcp_flags & 16 != 0 and tcp_flags & 3 == 0)
            data_arrived += int(direction == 0 and payload > 0)
        if direction == 0:
            queued.append((service_end, wire))
            occupancy += wire
            peak = max(peak, occupancy)
            queue_changes.extend(((capture, wire), (service_end, -wire)))
            overlap = max(0, min(service_end, end) - max(service_start, start))
            busy += overlap
            service_payload += Fraction(payload * overlap, service_end - service_start)
            service_retx += Fraction(payload * overlap * retx, service_end - service_start)
    assert highest == accepted and 0 <= peak <= 31250 and 0 <= busy <= end - start
    # Full queued packet bytes persist through service completion, matching the
    # declared queue capacity rather than the continuously shrinking byte tail.
    queue_changes.sort()
    area = current = window_peak = 0
    previous_time = 0
    queue_states = []
    for at, change in queue_changes:
        overlap = max(0, min(at, end) - max(previous_time, start))
        area += current * overlap
        if overlap:
            window_peak = max(window_peak, current)
        current += change
        assert 0 <= current <= 31250
        if start <= at <= end:
            window_peak = max(window_peak, current)
        queue_states.append((at, current))
        previous_time = at
    assert current == 0
    samples, measurements = value["Samples"], observed["Measurements"]
    assert 0 < len(samples) <= 601 and len(samples) == len(measurements)
    common, counters = [], []
    previous = None
    queue_index = 0
    actual_queue = 0
    for sample, measurement in zip(samples, measurements):
        at = sample["AtNS"]
        assert measurement["AtNS"] == at and sample["Flow"] == 0
        assert 0 <= sample["Acknowledged"] <= sample["Highest"] <= sample["Accepted"] <= accepted
        assert 0 <= sample["Delivered"] <= sample["Highest"]
        assert sample["AppRemaining"] == (64 << 20) - sample["Accepted"]
        assert 0 <= sample["Probe"]["AtNS"] <= at <= finish and sample["Probe"]["SackedOut"] >= 0
        assert sample["MSS"] > 0 and sample["Cwnd"] > 0
        for key in ("Timeouts", "Retransmits", "FastRetransmits"):
            assert type(measurement[key]) is int and measurement[key] >= 0
        if previous is not None:
            prior_sample, prior_counter = previous
            assert at > prior_sample["AtNS"]
            assert all(sample[key] >= prior_sample[key] for key in ("Accepted", "Delivered", "Highest", "Acknowledged"))
            assert all(measurement[key] >= prior_counter[key] for key in ("Timeouts", "Retransmits", "FastRetransmits"))
        previous = sample, measurement
        while queue_index < len(queue_states) and queue_states[queue_index][0] <= at:
            actual_queue = queue_states[queue_index][1]
            queue_index += 1
        assert measurement["QueuedBytes"] == actual_queue, (algorithm, at, "queue occupancy")
        if at >= end:
            assert sample["Accepted"] == accepted
        if start <= at <= end:
            assert sample["AppRemaining"] > 0 and sample["Accepted"] > sample["Acknowledged"]
            common.append(sample)
            counters.append(measurement)
    assert [sample["AtNS"] for sample in common] == list(range(start, end + 1, 100000000))
    for bucket in bins:
        left, right = [common[(at - start) // 100000000] for at in (bucket["start_ns"], bucket["end_ns"])]
        bucket["delivered_bytes"] = right["Delivered"] - left["Delivered"]
        bucket["acked_bytes"] = right["Acknowledged"] - left["Acknowledged"]
    active = False
    recovery_time = -1
    changes = value["RecoveryChanges"] or []
    for change in changes:
        assert change["Flow"] == 0 and type(change["Active"]) is bool and change["Active"] != active
        assert recovery_time <= change["AtNS"] <= finish and change["SackedOut"] >= 0
        active, recovery_time = change["Active"], change["AtNS"]
    window_changes = [change for change in changes if start < change["AtNS"] <= end]
    entries = [change["AtNS"] for change in window_changes if change["Active"]]
    delivered = common[-1]["Delivered"] - common[0]["Delivered"]
    acknowledged = common[-1]["Acknowledged"] - common[0]["Acknowledged"]
    assert delivered >= 0 and acknowledged >= 0
    return {"algorithm": algorithm, "window_ns": [start, end], "delivered_bytes": delivered,
            "acked_bytes": acknowledged, "delivery_mbit_s": delivered * 8 / 15e6,
            "ack_mbit_s": acknowledged * 8 / 15e6, "accepted_total_bytes": accepted,
            "full_drain_ns": finish, "payload_sha256": flow["PayloadSHA256"],
            "service_busy_ns": busy, "service_idle_ns": end - start - busy, "service_utilization": busy / (end - start),
            "service_payload_bytes_equivalent": {"numerator": service_payload.numerator, "denominator": service_payload.denominator},
            "service_retx_payload_bytes_equivalent": {"numerator": service_retx.numerator, "denominator": service_retx.denominator},
            "whole_queue_peak_bytes": peak, "window_queue_peak_bytes": window_peak,
            "window_queue_byte_ns": area, "window_mean_queue_bytes": area / (end - start),
            "window_counter_deltas": {key: counters[-1][key] - counters[0][key] for key in ("Timeouts", "Retransmits", "FastRetransmits")},
            "lifetime_stack_recovery": {key: value[key] for key in ("RTOs", "SACKRecoveries", "TLPRecoveries")},
            "samples": common, "counter_queue_samples": counters, "recovery_changes": changes,
            "window_recovery_entries_ns": entries, "repeated_observed_window_recovery": len(entries) >= 2,
            "drops": drops, "retransmissions": retransmissions, "second_bins": bins,
            "receiver_ack_packets_emitted": ack_emitted, "sender_ack_packets_arrived": ack_arrived,
            "data_packets_arrived": data_arrived, "event_count": len(events),
            "events_sha256": hashlib.sha256(json.dumps(events, separators=(",", ":")).encode()).hexdigest()}


def comparisons(observations: dict) -> dict:
    reno, cubic = [observations[ROOT + "/" + algorithm] for algorithm in ("reno", "cubic")]
    return {"window_ns": [5 * 10**9, 20 * 10**9],
            "cubic_minus_reno_delivered_bytes": cubic["delivered_bytes"] - reno["delivered_bytes"],
            "cubic_minus_reno_acked_bytes": cubic["acked_bytes"] - reno["acked_bytes"],
            "cubic_minus_reno_busy_ns": cubic["service_busy_ns"] - reno["service_busy_ns"],
            "both_have_repeated_observed_recovery": all(value["repeated_observed_window_recovery"] for value in (reno, cubic))}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [OWNER, OWNER + "_nogo"] or not config["matched_inputs"]:
        raise ValueError("Exact owners and nonempty frozen inputs are required")
    for path, expected in config["matched_inputs"].items():
        body = subprocess.check_output(["git", "show", config["source"] + ":" + path])
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError(f"Frozen input differs: {path}")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=all", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1"]
    result = {"status": "INCOMPLETE", "source": config["source"], "checks": [], "phases": {}}
    errors = []
    try:
        sdk = invoke("declared-go-sdk", ["run", *options, SDK, "--", "env", "-json", "GOROOT", "GOVERSION"],
                     "command", targets=[])
        result["checks"].append({"name": "declared-go-sdk", "result": sdk})
        # A formatter setup failure must not hide the remaining native tests.
        try:
            if sdk["status"] != "PASS":
                raise RuntimeError("Declared SDK query failed")
            env_path = evidence / "invocation-declared-go-sdk" / "stdout.log"
            match json.loads(env_path.read_text()):
                case {"GOROOT": str(root), "GOVERSION": "go1.27.3"}:
                    sdk_root = Path(root)
                case other:
                    raise ValueError(f"Unexpected SDK: {other}")
            if not sdk_root.is_absolute() or not sdk_root.is_dir():
                raise ValueError("Declared SDK root is not an existing absolute directory")
            result["sdk_environment"] = report.file_record(env_path)
            result["tools"] = {}
            for name in ("go", "gofmt"):
                path = sdk_root / "bin" / name
                if not path.is_file() or not os.access(path, os.X_OK):
                    raise ValueError(f"SDK tool is not executable: {path}")
                result["tools"][name] = report.file_record(path)
            original_path = os.environ["PATH"]
            try:
                os.environ["PATH"] = str(sdk_root / "bin") + os.pathsep + original_path
                selected_go = shutil.which("go")
                if selected_go is None or not (sdk_root / "bin" / "go").samefile(selected_go):
                    raise ValueError("Canonical lint would not use the declared Go SDK")
                formatted = invoke("gofmt", ["run", *options, PYTHON, "--", "-c",
                                   'import os, subprocess, sys; os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"]); sys.exit(subprocess.run(["bash", "tools/lint.sh", "gofmt"]).returncode)'],
                                   "command", targets=[])
                result["checks"].append({"name": "gofmt", "result": formatted})
            finally:
                os.environ["PATH"] = original_path
        except (OSError, ValueError, RuntimeError) as error:
            errors.append(str(error))
        normal = invoke("virtual-normal", ["test", *options, *selected, "--runs_per_test=2",
                         "--test_arg=-test.run=^" + ROOT + "$", OWNER], "test", targets=[OWNER])
        result["checks"].append({"name": "virtual-normal", "result": normal})
        race = invoke("virtual-race", ["test", *options, "--config=race", *selected, "--runs_per_test=1",
                       "--test_arg=-test.run=^" + ROOT + "$", OWNER], "test", targets=[OWNER])
        result["checks"].append({"name": "virtual-race", "result": race})
        nogo = invoke("virtual-nogo", ["test", *options, *selected, "--runs_per_test=1", OWNER + "_nogo"],
                      "test", targets=[OWNER + "_nogo"])
        result["checks"].append({"name": "virtual-nogo", "result": nogo})
        # All native test phases finish before any observation interpretation.
        errors.extend(row["name"] + ": native phase did not pass" for row in result["checks"]
                      if row["result"]["status"] != "PASS")
        if errors:
            raise RuntimeError("; ".join(errors))
        normal_observed = observations(evidence, "virtual-normal", 2)
        result["phases"]["virtual-normal"] = normal_observed
        race_observed = observations(evidence, "virtual-race", 1)
        result["phases"]["virtual-race"] = race_observed
        if race_observed["reference_sample"] != normal_observed["reference_sample"]:
            raise ValueError("Normal/race complete packet/state observations differ")
        if len(nogo["executed"]) != 1:
            raise ValueError("Expected exactly one owning analyzer attempt")
        attempt = nogo["executed"][0]
        if (attempt["owner"]["label"] != OWNER + "_nogo" or attempt["status"] != "PASSED"
                or attempt["cached_locally"] or (attempt["run"], attempt["shard"], attempt["attempt"]) != (1, 1, 1)
                or attempt["xml"] != {"failed": 0, "failures": [], "passed": 1, "skipped": 0, "skips": []}):
            raise ValueError("Unexpected analyzer native/XML census")
        directory = evidence / "invocation-virtual-nogo"
        manifest = json.loads((directory / "events.jsonl.manifest.json").read_text())
        event_body = (directory / "events.jsonl").read_bytes()
        if (len(event_body) != manifest["events"]["bytes"]
                or hashlib.sha256(event_body).hexdigest() != manifest["events"]["sha256"]):
            raise ValueError("Analyzer native event identity mismatch")
        native = [event for event in map(json.loads, event_body.splitlines())
                  if "testResult" in event.get("id", {})]
        if len(native) != 1:
            raise ValueError("Analyzer native attempt count differs")
        identity, native_row = native[0]["id"]["testResult"], native[0]["testResult"]
        if (identity != {"label": OWNER + "_nogo", "configuration": {"id": attempt["owner"]["configuration"]},
                         "run": 1, "attempt": 1, "shard": 1}
                or native_row["status"] != "PASSED" or native_row.get("cachedLocally", False)
                or native_row.get("executionInfo", {}).get("cachedRemotely", False)
                or native_row.get("executionInfo", {}).get("strategy") != "local"):
            raise ValueError("Analyzer native identity or execution differs")
        reports = manifest["reports"]
        if sorted(row["name"] for row in reports) != ["test.log", "test.xml"] or len({row["path"] for row in reports}) != 2:
            raise ValueError("Analyzer reports missing or share a path")
        for row in reports:
            body = (directory / row["path"]).read_bytes()
            outputs = [item for item in native_row["testActionOutput"] if item["name"] == row["name"]]
            if (row["identity"] != identity or len(body) != row["bytes"]
                    or hashlib.sha256(body).hexdigest() != row["sha256"] or len(outputs) != 1
                    or not outputs[0]["uri"].endswith("/" + row["path"].removeprefix("reports/"))):
                raise ValueError("Analyzer native report identity differs")
            if row["name"] == "test.xml":
                cases = ET.fromstring(body).findall(".//testcase")
                if len(cases) != 1 or any(list(case) for case in cases):
                    raise ValueError("Analyzer XML census differs")
        logs = [row for row in manifest["reports"] if row["name"] == "test.log"]
        if len(logs) != 1:
            raise ValueError("Expected one analyzer log")
        body = (directory / logs[0]["path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != logs[0]["sha256"] or len(body) != logs[0]["bytes"]:
            raise ValueError("Analyzer log identity mismatch")
        if b"amd64...PASS" not in body or b"arm64...PASS" not in body:
            raise ValueError("Analyzer did not affirm both architectures")
        result["observations"] = {name: check_observation(value)
                                  for name, value in normal_observed["reference_sample"].items()}
        result["comparisons"] = comparisons(result["observations"])
        result["status"] = "PASS"
    except Exception as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        report.write_json(evidence / "virtual-tcp-determinism.json", result)
