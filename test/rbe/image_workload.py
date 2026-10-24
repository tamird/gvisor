"""Compare single-flow Reno/CUBIC at exact common high-BDP recovery boundaries."""
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
ROOT = "TestVirtualTCPRecoveryWindow"
CASES = {f"{ROOT}/{algorithm}/{loss}" for algorithm in ("reno", "cubic") for loss in ("no_loss", "burst_loss")}
PARENTS = {ROOT, ROOT + "/reno", ROOT + "/cubic"}


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
            values = [json.loads(line.split("VIRTUAL_TCP ", 1)[1])
                      for line in body.decode().splitlines() if "VIRTUAL_TCP " in line]
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
    """Compare exact common-time delivery/ACK progress with clipped link service."""
    algorithm, loss = value["Case"].split("/")[-2:]
    assert value["Case"] in CASES and value["Algorithm"] == algorithm
    assert value["Bytes"] == value["AcceptedBytes"] == value["OfferedBytes"] == 64 << 20
    assert (value["BDPBytes"], value["RateBytesPerSecond"], value["OneWayDelayNS"]) == (1250000, 12500000, 50000000)
    finish = value["VirtualNS"]
    assert 5 * 10**9 < finish <= 30 * 10**9
    trace, rounds, holes = value["Trace"], value["Rounds"], value["Holes"] or []
    assert 0 < len(trace) <= 512 and 0 < len(rounds) <= 302
    assert value["PacketCount"] > len(trace) and len(value["AllPacketsSHA256"]) == 64
    assert hashlib.sha256(json.dumps(trace, separators=(",", ":")).encode()).hexdigest() == value["TraceSHA256"]
    assert [row for row in trace if row["Dropped"]] == holes
    assert len(holes) == (8 if loss == "burst_loss" else 0)
    repairs = []
    for index, hole in enumerate(holes):
        assert hole["From"] == 0 and not hole["Retransmitted"] and hole["PayloadBytes"] > 0
        if index:
            assert holes[index - 1]["EndSequence"] == hole["Sequence"]
        matches = [row for row in trace if row["From"] == 0 and row["Retransmitted"] and not row["Dropped"]
                   and (row["Sequence"], row["EndSequence"], row["PayloadSHA256"]) ==
                   (hole["Sequence"], hole["EndSequence"], hole["PayloadSHA256"])]
        assert matches, hole
        repairs.append({"sequence": hole["Sequence"], "end_sequence": hole["EndSequence"],
                        "bytes": hole["PayloadBytes"], "payload_sha256": hole["PayloadSHA256"],
                        "drop_capture_ns": hole["CapturedAtNS"], "drop_at_ns": hole["AtNS"],
                        "retransmission_capture_ns": [row["CapturedAtNS"] for row in matches],
                        "retransmission_arrival_ns": [row["AtNS"] for row in matches]})
    syns = [row for row in trace if row["Flags"] & 2]
    assert len(syns) == 2 and {row["From"] for row in syns} == {0, 1} and all(row["SACKPermitted"] for row in syns)
    for previous, current in zip(rounds, rounds[1:]):
        for key in ("AtNS", "DeliveredBytes", "AcceptedBytes", "CumulativelyAcknowledgedBytes"):
            assert previous[key] <= current[key], (key, previous, current)
    final = rounds[-1]
    assert final["AcceptedBytes"] == final["DeliveredBytes"] == final["CumulativelyAcknowledgedBytes"] == 64 << 20
    assert final["AppRemainingBytes"] == 0 and final["Probe"]["SackedOut"] == 0
    assert all(row["Probe"]["SackedOut"] >= 0 for row in rounds)
    if loss == "no_loss":
        assert value["Retransmits"] == value["RTOs"] == 0
        assert value["PreBurst"] is None and value["HolesAcknowledgedNS"] == 0 and value["DropOffset"] == -1
    else:
        assert value["DropOffset"] == 16 << 20 and 0 < value["HolesAcknowledgedNS"] < finish
        assert value["PreBurst"]["AppRemainingBytes"] > 0
    window = value["Window"]
    first, last = window["Start"], window["End"]
    start, end = first["AtNS"], last["AtNS"]
    assert (start, end) == (3 * 10**9, 5 * 10**9)
    assert value["DemandEndedNS"] > end
    for edge in (first, last):
        assert edge["OfferedBytes"] == 64 << 20
        assert edge["AppRemainingBytes"] == (64 << 20) - edge["AcceptedBytes"] > 0
        assert edge["AcceptedBytes"] > edge["CumulativelyAcknowledgedBytes"]
        assert 0 <= edge["CumulativelyAcknowledgedBytes"] <= edge["DeliveredBytes"] <= edge["AcceptedBytes"]
        assert edge["MSS"] > 0 and edge["Cwnd"] > 0 and edge["Probe"]["AtNS"] <= edge["AtNS"]
    delta_keys = ("DeliveredBytes", "CumulativelyAcknowledgedBytes", "CapturedSequenceBytes", "AcceptedBytes",
                  "RTOs", "Retransmits", "SACKRecoveries", "TLPRecoveries")
    deltas = {key: last[key] - first[key] for key in delta_keys}
    assert all(number >= 0 for number in deltas.values())
    totals = [{"service_ns": 0, "wire_byte_ns": 0, "payload_byte_ns": 0, "retx_payload_byte_ns": 0,
               "captured_data_packets": 0, "captured_data_bytes": 0, "captured_retx_packets": 0,
               "captured_retx_bytes": 0, "captured_drop_packets": 0, "captured_ack_packets": 0,
               "arrived_data_packets": 0, "arrived_data_bytes": 0, "arrived_ack_packets": 0} for _ in range(2)]
    previous_capture = -1
    service_ends = [0, 0]
    rows = window["Packets"]
    assert 0 < len(rows) <= 60000
    for row in rows:
        match row:
            case [side, capture, service_start, service_end, arrival, wire, payload, retx, drop, flags, ack] if all(type(x) is int for x in row):
                pass
            case _:
                raise ValueError(f"Malformed window packet: {row}")
        assert side in (0, 1) and retx in (0, 1) and drop in (0, 1)
        assert previous_capture <= capture <= service_start < service_end < arrival <= finish
        assert 40 <= wire <= 1500 and 0 <= payload <= wire - 40 and 0 <= flags <= 255 and 0 <= ack < 2**32
        assert service_ends[side] <= service_start
        assert service_end - service_start == (wire * 10**9 + 12500000 - 1) // 12500000
        assert arrival - service_end == 50000000
        assert (start <= capture <= end or service_start < end and service_end > start or start <= arrival <= end)
        assert side == 0 or payload == retx == drop == 0
        previous_capture, service_ends[side] = capture, service_end
        busy = max(0, min(end, service_end) - max(start, service_start))
        total = totals[side]
        total["service_ns"] += busy
        # Each byte costs exactly80ns at this rate: clipping remains rational.
        assert service_end - service_start == wire * 80
        total["wire_byte_ns"] += busy
        total["payload_byte_ns"] += Fraction(payload * busy, wire)
        total["retx_payload_byte_ns"] += Fraction(payload * busy * retx, wire)
        if start < capture <= end:
            total["captured_data_packets"] += int(payload > 0)
            total["captured_data_bytes"] += payload
            total["captured_retx_packets"] += retx
            total["captured_retx_bytes"] += payload * retx
            total["captured_drop_packets"] += drop
            total["captured_ack_packets"] += int(payload == 0 and flags & 16 != 0 and flags & 3 == 0)
        if start < arrival <= end and not drop:
            total["arrived_data_packets"] += int(payload > 0)
            total["arrived_data_bytes"] += payload
            total["arrived_ack_packets"] += int(payload == 0 and flags & 16 != 0 and flags & 3 == 0)
    for total in totals:
        assert 0 <= total["service_ns"] <= end - start
        total["service_utilization"] = total["service_ns"] / (end - start)
        for key in ("wire", "payload", "retx_payload"):
            amount = Fraction(total.pop(key + "_byte_ns"), 80)
            total["service_" + key + "_bytes"] = {"numerator": amount.numerator, "denominator": amount.denominator}
    return {"algorithm": algorithm, "loss": loss, "window_ns": [start, end], "boundaries": [first, last],
            "deltas": deltas, "delivery_mbit_s": deltas["DeliveredBytes"] * 8 / 2e6,
            "ack_progress_mbit_s": deltas["CumulativelyAcknowledgedBytes"] * 8 / 2e6,
            "directions": totals, "retained_window_packets": len(rows),
            "window_packets_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
            "exact_holes": repairs, "holes_acknowledged_ns": value["HolesAcknowledgedNS"],
            "pre_burst": value["PreBurst"], "virtual_end_ns": finish, "demand_ended_ns": value["DemandEndedNS"],
            "lifetime_recovery": {key: value[key] for key in ("RTOs", "Retransmits", "SACKRecoveries", "TLPRecoveries")},
            "payload_sha256": value["PayloadSHA256"], "all_packets_sha256": value["AllPacketsSHA256"],
            "trace_sha256": value["TraceSHA256"], "packet_count": value["PacketCount"],
            "final": final}


def comparisons(observed: dict) -> list:
    rows = []
    for loss in ("no_loss", "burst_loss"):
        reno, cubic = [observed[f"{ROOT}/{algorithm}/{loss}"] for algorithm in ("reno", "cubic")]
        assert reno["payload_sha256"] == cubic["payload_sha256"]
        rows.append({"loss": loss, "window_ns": [3 * 10**9, 5 * 10**9],
                     "cubic_minus_reno_delivered_bytes": cubic["deltas"]["DeliveredBytes"] - reno["deltas"]["DeliveredBytes"],
                     "cubic_minus_reno_acked_bytes": cubic["deltas"]["CumulativelyAcknowledgedBytes"] - reno["deltas"]["CumulativelyAcknowledgedBytes"],
                     "reno": reno, "cubic": cubic})
    return rows


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
        for algorithm in ("reno", "cubic"):
            phase = "virtual-race-" + algorithm
            race = invoke(phase, ["test", *options, "--config=race", *selected, "--runs_per_test=1",
                          "--test_arg=-test.run=^" + ROOT + "$/^" + algorithm + "$/^burst_loss$", OWNER],
                          "test", targets=[OWNER])
            result["checks"].append({"name": phase, "result": race})
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
        for algorithm in ("reno", "cubic"):
            phase = "virtual-race-" + algorithm
            case = f"{ROOT}/{algorithm}/burst_loss"
            race_observed = observations(evidence, phase, 1, {case}, {ROOT, ROOT + "/" + algorithm})
            result["phases"][phase] = race_observed
            if race_observed["reference_sample"] != {case: normal_observed["reference_sample"][case]}:
                raise ValueError(f"{algorithm}: normal/race complete observations differ")
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
