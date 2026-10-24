"""Measure the reviewed RACK correction on the retained two-case FIFO carrier."""
from collections import deque
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
ROOT = "TestVirtualTCPRecoveryDiagnostic"
LEAVES = {"cubic_reno_setup01_write01", "cubic_reno_setup01_write10"}
CASES = {ROOT + "/" + name for name in LEAVES}
PARENTS = {ROOT}
# Authenticated retained baseline gate and first admitted payloads; later
# traffic is deliberately allowed to differ with the production correction.
BASELINE = {'TestVirtualTCPRecoveryDiagnostic/cubic_reno_setup01_write01': {'pre_gate_sha256': 'f86b1570a1a472fdc940a043dcc57a35ab08c5309f204a7ae45e741168e06a3e',
                                                                 'writes': [{'Accepted': 1048576,
                                                                             'AtNS': 250000000,
                                                                             'Flow': 0},
                                                                            {'Accepted': 1048576,
                                                                             'AtNS': 250000000,
                                                                             'Flow': 1}],
                                                                 'first_admitted': [[6,
                                                                                     250000000,
                                                                                     0,
                                                                                     0,
                                                                                     1472,
                                                                                     1420,
                                                                                     271965322,
                                                                                     943095740,
                                                                                     250000000,
                                                                                     251177600,
                                                                                     301177600,
                                                                                     0,
                                                                                     0],
                                                                                    [16,
                                                                                     250000000,
                                                                                     0,
                                                                                     1,
                                                                                     1472,
                                                                                     1420,
                                                                                     3529902345,
                                                                                     3074080200,
                                                                                     261776000,
                                                                                     262953600,
                                                                                     312953600,
                                                                                     0,
                                                                                     0]]},
 'TestVirtualTCPRecoveryDiagnostic/cubic_reno_setup01_write10': {'pre_gate_sha256': 'f86b1570a1a472fdc940a043dcc57a35ab08c5309f204a7ae45e741168e06a3e',
                                                                 'writes': [{'Accepted': 1048576,
                                                                             'AtNS': 250000000,
                                                                             'Flow': 1},
                                                                            {'Accepted': 1048576,
                                                                             'AtNS': 250000000,
                                                                             'Flow': 0}],
                                                                 'first_admitted': [[16,
                                                                                     250000000,
                                                                                     0,
                                                                                     0,
                                                                                     1472,
                                                                                     1420,
                                                                                     271965322,
                                                                                     943095740,
                                                                                     261776000,
                                                                                     262953600,
                                                                                     312953600,
                                                                                     0,
                                                                                     0],
                                                                                    [6,
                                                                                     250000000,
                                                                                     0,
                                                                                     1,
                                                                                     1472,
                                                                                     1420,
                                                                                     3529902345,
                                                                                     3074080200,
                                                                                     250000000,
                                                                                     251177600,
                                                                                     301177600,
                                                                                     0,
                                                                                     0]]}}


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
    """Replay the declared FIFO and separate interval service from later delivery."""
    case = value["Case"].removeprefix(ROOT + "/")
    if case not in LEAVES:
        raise ValueError(f"Unknown shared-FIFO case: {case}")
    first, second, setup, write = case.split("_")
    algorithms, order, stagger = [first, second], [int(x) for x in setup.removeprefix("setup")], 0
    first_order = [int(x) for x in write.removeprefix("write")]
    expected = {"Algorithms": algorithms, "Order": order, "StaggerNS": stagger,
                "RateBytesPerSecond": 1250000, "OneWayDelayNS": 50000000,
                "CapacityBytes": 125000, "WindowStartNS": 2 * 10**9, "WindowEndNS": 6 * 10**9}
    if {key: value[key] for key in expected} != expected:
        raise ValueError(f"{case}: scenario differs from frozen selection")
    application = value["ApplicationOrder"]
    gate = 250000000
    if (application["FirstWriteOrder"] != first_order or application["DemandAtNS"] != gate
            or application["PreGatePending"] != 0 or application["PreGateQueuedBytes"] != 0
            or len(application["PreGateNextTXNS"]) != 2
            or any(not 0 <= x < gate for x in application["PreGateNextTXNS"])
            or len(application["PreGateConnectedNS"]) != 2
            or any(not 0 < x < gate for x in application["PreGateConnectedNS"])):
        raise ValueError(f"{case}: common established/empty gate differs")
    writes = application["Writes"]
    if (len(writes) != 2 or [row["Flow"] for row in writes] != first_order
            or any(row["AtNS"] != gate or not 0 < row["Accepted"] <= 1 << 20 for row in writes)):
        raise ValueError(f"{case}: first successful application writes differ")
    senders = application["PreGateSenders"]
    if (len(senders) != 2 or [row["Flow"] for row in senders] != [0, 1]
            or any(row["AtNS"] != gate or row["AppRemaining"] != 16 << 20
                   or row["Cwnd"] <= 0 or row["MSS"] <= 0
                   or any(row[key] != 0 for key in ("Accepted", "Delivered", "Highest", "Acknowledged"))
                   for row in senders)):
        raise ValueError(f"{case}: initial sender state differs")
    pre_count = application["PreGateEventCount"]
    pre_events = value["Events"][:pre_count]
    packets = application["PreGatePackets"]
    if (pre_count != 6 or len(packets) != pre_count
            or any(row[1] >= gate or row[5] != 0 or row[11] != 0 for row in pre_events)
            or any(len(bytes.fromhex(packet)) != row[4] for packet, row in zip(packets, pre_events))):
        raise ValueError(f"{case}: complete pre-gate handshake evidence differs")
    start, end = value["WindowStartNS"], value["WindowEndNS"]
    finish, count = value["VirtualEndNS"], len(algorithms)
    flows = value["Flows"]
    if not end <= finish <= 30 * 10**9 or len(flows) != count:
        raise ValueError(f"{case}: completion bounds/flow count differ")
    summaries = []
    for index, flow in enumerate(flows):
        accepted = flow["Accepted"]
        if (flow["Algorithm"] != algorithms[index] or flow["LocalPort"] != 40000 + index
                or flow["RemotePort"] != 8080 + index or not 0 < accepted < 16 << 20
                or flow["Delivered"] != accepted or flow["Acknowledged"] != accepted
                or flow["EOF"] is not True or flow["FinWait2"] is not True
                or not order.index(index) * stagger <= flow["ConnectedNS"] <= flow["StartedNS"] < start):
            raise ValueError(f"{case}: flow {index} did not enter and complete the declared transfer")
        period = bytes((offset * 17 + offset // 251 + index * 79) % 256 for offset in range(251 * 256))
        payload = (period * ((accepted + len(period) - 1) // len(period)))[:accepted]
        if hashlib.sha256(payload).hexdigest() != flow["PayloadSHA256"]:
            raise ValueError(f"{case}: flow {index} delivered payload identity differs")
        summaries.append({"algorithm": algorithms[index], "accepted_bytes": accepted,
                          "service_ns": 0, "service_wire_bytes_equivalent": 0.0,
                          "service_payload_bytes_equivalent": 0.0, "whole_drops": 0,
                          "window_drops": 0, "whole_retransmissions": 0, "window_retransmissions": 0,
                          "equal_time_first": 0, "recovery_entries": 0})
    next_tx = [0, 0]
    queued = deque()
    occupancy = 0
    peak = 0
    highest = [0] * count
    previous_capture = -1
    equal_time_flows = {}
    events = value["Events"]
    if not events or len(events) > 50000:
        raise ValueError(f"{case}: missing or excessive packet evidence")
    for ordinal, row in enumerate(events):
        match row:
            case [number, capture, direction, flow, wire, payload, sequence, ack,
                  service_start, service_end, arrival, dropped, retransmitted] if all(type(x) is int for x in row):
                pass
            case _:
                raise ValueError(f"{case}: malformed packet row {ordinal}")
        if (number != ordinal or not previous_capture <= capture <= finish or direction not in (0, 1)
                or not 0 <= flow < count or not 40 <= wire <= 1500 or not 0 <= payload <= wire - 40
                or not 0 <= sequence < 2**32 or not 0 <= ack < 2**32
                or dropped not in (0, 1) or retransmitted not in (0, 1)):
            raise ValueError(f"{case}: packet identity/range differs at {ordinal}")
        previous_capture = capture
        while queued and queued[0][0] <= capture:
            occupancy -= queued.popleft()[1]
        should_drop = direction == 0 and occupancy + wire > value["CapacityBytes"]
        if dropped != int(should_drop):
            raise ValueError(f"{case}: admission differs at {ordinal}")
        summary = summaries[flow]
        if direction == 0 and payload:
            offset = (sequence - flows[flow]["DataSequence"]) % 2**32
            if (offset + payload > flows[flow]["Accepted"]
                    or retransmitted != int(offset < highest[flow])):
                raise ValueError(f"{case}: sequence/retransmission range differs at {ordinal}")
            highest[flow] = max(highest[flow], offset + payload)
            if start <= capture < end:
                equal_time_flows.setdefault(capture, []).append(flow)
        elif retransmitted or direction == 1 and payload:
            raise ValueError(f"{case}: unexpected reverse payload or retransmission flag")
        if direction == 0:
            summary["whole_drops"] += dropped
            summary["whole_retransmissions"] += retransmitted
            if start <= capture < end:
                summary["window_drops"] += dropped
                summary["window_retransmissions"] += retransmitted
        if dropped:
            if (service_start, service_end, arrival) != (-1, -1, -1):
                raise ValueError(f"{case}: dropped packet consumed service")
            continue
        wanted_start = max(next_tx[direction], capture)
        wanted_end = wanted_start + (wire * 10**9 + value["RateBytesPerSecond"] - 1) // value["RateBytesPerSecond"]
        if (service_start, service_end, arrival) != (wanted_start, wanted_end, wanted_end + value["OneWayDelayNS"]):
            raise ValueError(f"{case}: serializer schedule differs at {ordinal}")
        if arrival > finish:
            raise ValueError(f"{case}: admitted packet remains beyond native completion")
        next_tx[direction] = service_end
        if direction == 0:
            queued.append((service_end, wire))
            occupancy += wire
            peak = max(peak, occupancy)
            overlap = max(0, min(service_end, end) - max(service_start, start))
            summary["service_ns"] += overlap
            summary["service_wire_bytes_equivalent"] += wire * overlap / (service_end - service_start)
            summary["service_payload_bytes_equivalent"] += payload * overlap / (service_end - service_start)
    if highest != [flow["Accepted"] for flow in flows]:
        raise ValueError(f"{case}: final observed sequence ranges differ from accepted bytes")
    tied = 0
    for queue in equal_time_flows.values():
        if len(set(queue)) > 1:
            tied += 1
            summaries[queue[0]]["equal_time_first"] += 1
    by_flow = [[] for _ in flows]
    for sample in value["Samples"]:
        index = sample["Flow"]
        if type(index) is not int or not 0 <= index < count:
            raise ValueError(f"{case}: sample flow differs")
        by_flow[index].append(sample)
    for index, samples in enumerate(by_flow):
        common = [sample for sample in samples if start <= sample["AtNS"] <= end]
        if [sample["AtNS"] for sample in common] != list(range(start, end + 1, 100000000)):
            raise ValueError(f"{case}: flow {index} lacks exact common-window sample coverage")
        previous = None
        for sample in samples:
            if (not 0 <= sample["Acknowledged"] <= sample["Highest"] <= sample["Accepted"] <= flows[index]["Accepted"]
                    or not 0 <= sample["Delivered"] <= sample["Highest"]
                    or sample["AppRemaining"] != (16 << 20) - sample["Accepted"]
                    or not 0 <= sample["Probe"]["AtNS"] <= sample["AtNS"] <= finish
                    or sample["Probe"]["SackedOut"] < 0 or sample["MSS"] <= 0 or sample["Cwnd"] <= 0):
                raise ValueError(f"{case}: flow {index} sample bounds differ")
            if previous is not None and (sample["AtNS"] <= previous["AtNS"] or any(
                    sample[key] < previous[key] for key in ("Accepted", "Delivered", "Highest", "Acknowledged"))):
                raise ValueError(f"{case}: flow {index} sample progress is not monotonic")
            if sample["AtNS"] >= end and sample["Accepted"] != flows[index]["Accepted"]:
                raise ValueError(f"{case}: application demand changed after the common interval")
            previous = sample
        if any(sample["AppRemaining"] <= 0 or sample["Accepted"] <= sample["Acknowledged"] for sample in common):
            raise ValueError(f"{case}: flow {index} was not backlogged throughout the window")
        summaries[index].update(
            window_delivered_bytes=common[-1]["Delivered"] - common[0]["Delivered"],
            prewindow_delivered_bytes=common[0]["Delivered"],
            drain_delivered_bytes=flows[index]["Delivered"] - common[-1]["Delivered"],
            window_acked_bytes=common[-1]["Acknowledged"] - common[0]["Acknowledged"],
            samples=common)
    active = [False] * count
    recovery_time = -1
    for change in value["RecoveryChanges"] or []:
        index = change["Flow"]
        if (type(index) is not int or not 0 <= index < count or type(change["Active"]) is not bool
                or change["Active"] == active[index] or not recovery_time <= change["AtNS"] <= finish
                or change["SackedOut"] < 0):
            raise ValueError(f"{case}: recovery transition differs")
        recovery_time = change["AtNS"]
        active[index] = change["Active"]
        if start <= change["AtNS"] < end and change["Active"]:
            summaries[index]["recovery_entries"] += 1
    totals = {key: value[key] for key in ("RTOs", "SACKRecoveries", "TLPRecoveries")}
    if any(type(x) is not int or x < 0 for x in totals.values()):
        raise ValueError(f"{case}: aggregate recovery counters differ")
    busy = sum(flow["service_ns"] for flow in summaries)
    if not 0 < busy <= end - start:
        raise ValueError(f"{case}: invalid shared service occupancy")
    delivered = sum(flow["window_delivered_bytes"] for flow in summaries)
    if delivered <= 0:
        raise ValueError(f"{case}: common contention interval delivered no data")
    for flow in summaries:
        flow["window_delivery_share"] = flow["window_delivered_bytes"] / delivered
    first_payload = {}
    first_admitted = {}
    for row in events:
        if row[2] == 0 and row[5] > 0:
            first_payload.setdefault(row[3], row)
            if not row[11]:
                first_admitted.setdefault(row[3], row)
    if (set(first_payload) != {0, 1} or set(first_admitted) != {0, 1}
            or any(flow["StartedNS"] != gate for flow in flows)
            or any(row[1] < gate for row in first_payload.values())):
        raise ValueError(f"{case}: first payload evidence differs")
    trace = json.dumps(events, separators=(",", ":")).encode()
    gate_state = {key: item for key, item in application.items() if key not in ("FirstWriteOrder", "Writes")}
    gate_state["Events"] = pre_events
    return {"case": case, "flows": summaries, "window_ns": [start, end],
            "application_order": application, "pre_gate": gate_state,
            "packet_trace_sha256": hashlib.sha256(trace).hexdigest(),
            "pre_gate_sha256": hashlib.sha256(json.dumps(gate_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "first_payload_events": [first_payload[index] for index in (0, 1)],
            "first_admitted_payload_events": [first_admitted[index] for index in (0, 1)],
            "actual_first_payload_flow": min(first_payload, key=lambda index: first_payload[index][0]),
            "actual_first_admitted_payload_flow": min(first_admitted, key=lambda index: first_admitted[index][0]),
            "service_busy_ns": busy, "service_idle_ns": end - start - busy,
            "service_utilization": busy / (end - start), "queue_peak_bytes": peak,
            "mixed_equal_time_batches": tied, "aggregate_lifetime_recovery": totals,
            "event_count": len(events), "virtual_end_ns": finish,
            "scope": "2–6 s contention; clipped FIFO service and interval delivery are distinct from later drain. Equal-time order is observed, not randomized. Aggregate recovery counters are not per-flow."}



def comparisons(observed: dict) -> list[dict]:
    """Matched comparisons report a NIC-order non-effect without failing it."""
    pairs = []
    for group in ("cubic_reno_setup01",):
        before, after = [observed[ROOT + "/" + group + "_write" + suffix] for suffix in ("01", "10")]
        if before["pre_gate"] != after["pre_gate"]:
            raise ValueError(f"{group}: setup packet/state evidence changed with application order")
        accepted = [{row["Flow"]: row["Accepted"] for row in arm["application_order"]["Writes"]}
                    for arm in (before, after)]
        if accepted[0] != accepted[1]:
            raise ValueError(f"{group}: initial offered/accepted demand differs")
        pairs.append({"pair": group, "pre_gate_sha256": before["pre_gate_sha256"],
                      "first_payload_order_changed": before["actual_first_payload_flow"] != after["actual_first_payload_flow"],
                      "first_admitted_payload_order_changed": before["actual_first_admitted_payload_flow"] != after["actual_first_admitted_payload_flow"],
                      "packet_traces_equal": before["packet_trace_sha256"] == after["packet_trace_sha256"],
                      "flow0_delivery_share_write01": before["flows"][0]["window_delivery_share"],
                      "flow0_delivery_share_write10": after["flows"][0]["window_delivery_share"],
                      "flow0_delivery_share_delta": after["flows"][0]["window_delivery_share"] - before["flows"][0]["window_delivery_share"],
                      "delivery_bytes_write01": [row["window_delivered_bytes"] for row in before["flows"]],
                      "delivery_bytes_write10": [row["window_delivered_bytes"] for row in after["flows"]],
                      "utilization_write01": before["service_utilization"],
                      "utilization_write10": after["service_utilization"],
                      "limits": "Only the first application Write batch changes; full finite trace retained. No required effect or algorithm winner; no steady-state/native throughput inference."})
    return pairs


def check_diagnostic(value: dict) -> dict:
    """Validate the unchanged observer without requiring unchanged recovery."""
    name = value["Case"].removeprefix(ROOT + "/")
    diagnostic = value["Diagnostic"]
    if set(diagnostic) != {"Probes", "State", "Wire"}:
        raise ValueError(f"{name}: diagnostic shape differs")
    expected_wire = [row[0] for row in value["Events"]
                     if row[3] == 1 and 600000000 <= row[1] <= 6000000000]
    if [row["Event"] for row in diagnostic["Wire"]] != expected_wire:
        raise ValueError(f"{name}: diagnostic wire coverage differs")
    for kind, rows in diagnostic.items():
        if not rows or len(rows) > 10000:
            raise ValueError(f"{name}: missing or excessive {kind}")
        previous = -1
        for row in rows:
            if kind == "Wire":
                ordinal = row["Event"]
                if type(ordinal) is not int or not 0 <= ordinal < len(value["Events"]):
                    raise ValueError(f"{name}: invalid wire ordinal")
                event = value["Events"][ordinal]
                at = event[1]
                if (event[0] != ordinal or event[3] != 1 or not 0 <= row["Flags"] < 256
                        or not 0 <= row["Window"] < 65536):
                    raise ValueError(f"{name}: wrong wire identity")
            else:
                at = row["AtNS"]
            if not 600000000 <= at <= 6000000000 or at < previous:
                raise ValueError(f"{name}: diagnostic time/order differs")
            previous = at
    for before, after in zip(diagnostic["State"], diagnostic["State"][1:]):
        if any(after[key] < before[key] for key in ("Timeouts", "Retransmits", "FastRetransmits")):
            raise ValueError(f"{name}: per-endpoint counters went backwards")
    return {"flow": 1,
            "algorithm": "reno", "observed": diagnostic,
            "scope": "Existing post-ACK probe and post-fence public endpoint counters/TCPInfo, plus original TCP options. No production hook or required diagnosis."}


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
                         "--test_arg=-test.run=^" + ROOT + "$/^(" + "|".join(sorted(LEAVES)) + ")$", OWNER],
                        "test", targets=[OWNER])
        result["checks"].append({"name": "virtual-normal", "result": normal})
        race = invoke("virtual-race", ["test", *options, "--config=race", *selected, "--runs_per_test=1",
                       "--test_arg=-test.run=^" + ROOT + "$/^(" + "|".join(sorted(LEAVES)) + ")$", OWNER],
                      "test", targets=[OWNER])
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
        race_observed = observations(evidence, "virtual-race", 1)
        result["phases"] = {"virtual-normal": normal_observed, "virtual-race": race_observed}
        if normal_observed["reference_sample"] != race_observed["reference_sample"]:
            raise ValueError("Normal/race complete packet and state observations differ")
        result["diagnostics"] = {name: check_diagnostic(value)
                                 for name, value in normal_observed["reference_sample"].items()}
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
        for name, observed in result["observations"].items():
            actual = {"pre_gate_sha256": observed["pre_gate_sha256"],
                      "writes": observed["application_order"]["Writes"],
                      "first_admitted": observed["first_admitted_payload_events"]}
            if actual != BASELINE[name]:
                raise ValueError(f"{name}: baseline gate, offered demand or first admissions changed")
        result["baseline_gate_match"] = True
        result["status"] = "PASS"
    except Exception as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        report.write_json(evidence / "virtual-tcp-determinism.json", result)
