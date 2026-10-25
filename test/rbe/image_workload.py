"""Replay the first finite-FIFO loss flight under two real recovery modes."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
TCP_NOGO = "//pkg/tcpip/transport/tcp:tcp_test_nogo"
ROOT = "TestVirtualTCPFrozenRecoveryFlight"
ROLES = ("reference", "rack_replay", "sack_replay")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def common_event(event: dict) -> dict:
    if "Endpoint" not in event:
        return event
    # Only RACK maintains this internal SACK packet counter. Wire SACK ranges
    # and both endpoint scoreboards remain in the common-state comparison.
    return {**event, "Endpoint": {**event["Endpoint"], "Sacked": 0}}


def repair(hole: dict, inputs: list[dict]) -> dict:
    start, end = hole["Seq"], hole["End"]
    intervals = []
    repaired_at = cumulative_at = None
    for wire in inputs:
        if wire["Side"] == 1:
            if cumulative_at is None and wire["Ack"] >= end:
                cumulative_at = wire["Arrival"]
            continue
        if not wire["Retransmit"] or wire["End"] <= start or end <= wire["Seq"]:
            continue
        intervals.append((max(start, wire["Seq"]), min(end, wire["End"])))
        if repaired_at is None:
            contiguous = start
            for left, right in sorted(intervals):
                if left > contiguous:
                    break
                contiguous = max(contiguous, right)
            if contiguous == end:
                repaired_at = wire["Arrival"]
    return {"first": start, "end": end, "repair_ns": repaired_at, "cumulative_ack_ns": cumulative_at}


def measure(samples: dict) -> dict:
    arms = {name.rsplit("/", 1)[1]: sample for name, sample in samples.items()}
    roles = set(arms)
    if roles not in ({ROLES[0]}, set(ROLES[:2]), set(ROLES)):
        raise ValueError(f"Unexpected flight roles {roles}")
    output = {}
    for role in ROLES:
        if role not in arms:
            continue
        sample = arms[role]
        flight = sample["Flight"]
        events = flight["Events"] or []
        last = sample["Rounds"][-1]
        startup = sample["Startup"]
        if (flight["Role"] != role or sample["Algorithm"] != "cubic" or startup["MSS"] != 1420
                or not all(ack["Plus"] for ack in startup["ACKs"])
                or sample["OfferedBytes"] != 16 << 20 or sample["OneWayDelayNS"] != 50_000_000
                or flight["ForwardRate"] != sample["RateBytesPerSecond"] or flight["ForwardRate"] != 1_250_000
                or flight["QueueCapacity"] != 125_000 or sample["BDPBytes"] != 125_000
                or flight["ReceiverMode"] != 1 or flight["SenderMode"] != int(role != "sack_replay")
                or flight["EndNS"] + startup["FirstPayloadNS"] != sample["VirtualNS"]
                or not 0 < sample["VirtualNS"] <= 30_000_000_000
                or last["CumulativelyAcknowledgedBytes"] != flight["EndAck"]):
            raise ValueError(f"{role}: recovery setting, link, demand, clock or endpoint differs")
        unmatched = flight["Match"] == "unmatched_prefix"
        if not unmatched:
            if (flight["SenderLatchedMode"] != flight["SenderMode"] or
                    {e["Endpoint"]["Side"] for e in events if "Endpoint" in e} != {0, 1}
                    or not all(e["Endpoint"]["Timestamps"] and e["Endpoint"]["SACKEnabled"]
                               for e in events if "Endpoint" in e)):
                raise ValueError(f"{role}: negotiated SACK, timestamps or active recovery mode unverified")
        holes = flight["Holes"] or []
        if role == "reference" and not unmatched:
            cutoff = flight["ReferenceCut"]
            onset = events[cutoff - 1]
            queue = sample["ForwardQueue"]
            if (flight["Match"] != "reference" or flight["End"] != "reference_first_recovery"
                    or not holes or holes[0]["Seq"] != 335 * 1420 or holes[0]["Captured"] != 523_572_800
                    or onset["At"] != 726_122_240 or onset["Action"]["Trigger"]["Ack"] != 335 * 1420
                    or flight["Originals"][-1]["End"] != flight["Fence"]
                    or queue["Rate"] != flight["ForwardRate"] or queue["Capacity"] != flight["QueueCapacity"]):
                raise ValueError("Reference does not reproduce historical loss and recovery onset")
        elif role != "reference" and not unmatched:
            ref = arms["reference"]["Flight"]
            if flight["Originals"] != ref["Originals"] or holes != ref["Holes"] or flight["Fence"] != ref["Fence"]:
                raise ValueError(f"{role}: byte ranges, original timing, windows, payload or FIFO exposure differ")
            if any(e["Wire"]["Dropped"] and (e["Wire"]["Retransmit"] or e["Wire"] not in holes)
                   for e in events if e["Kind"] == "offer"):
                raise ValueError(f"{role}: unexpected loss after reference cut")
            if flight["End"] == "flight_acknowledged":
                if flight["EndAck"] < ref["Fence"]:
                    raise ValueError(f"{role}: completion before cumulative fence")
            elif flight["End"] != "absolute_cap" or sample["VirtualNS"] != 30_000_000_000 or flight["EndAck"] >= ref["Fence"]:
                raise ValueError(f"{role}: neither completion nor the specified absolute censor time")
            if role == "rack_replay":
                if flight["Match"] != "reference_prefix_matched" or events[:ref["ReferenceCut"]] != ref["Events"]:
                    raise ValueError("RACK replay differs from its authentic reference")
            else:
                rack = arms["rack_replay"]["Flight"]
                i = flight["SplitIndex"] if flight["Match"] == "matched_decision" else len(events)
                if (list(map(common_event, events[:i])) != list(map(common_event, rack["Events"][:i]))
                        or (flight["Match"] == "matched_decision" and
                            (flight["Expected"] != rack["Events"][i] or flight["Actual"] != events[i]))
                        or (flight["Match"] == "no_observed_mode_divergence" and events != rack["Events"]
                            and list(map(common_event, events)) != list(map(common_event, rack["Events"])))
                        or flight["Match"] not in ("matched_decision", "no_observed_mode_divergence")):
                    raise ValueError("Alternate mode has no common prefix up to the stated decision")
        inputs = [e["Wire"] for e in events if e["Kind"] == "input"]
        repairs = [repair(hole, inputs) for hole in holes]
        acts = [e for e in events if "Action" in e and e["Action"]["Path"] not in ("ack_begin", "ack_end")]
        packets = [e["Wire"] for e in events if e["Kind"] == "offer" and e["Wire"]["Side"] == 0]
        post = [p for p in packets if p["Retransmit"] and p["Captured"] >= 726_122_240]
        output[role] = {"match": flight["Match"], "end": flight["End"], "events": len(events),
                        "sample_sha256": digest(sample), "originals_sha256": digest(flight["Originals"]),
                        "original_packets": len(flight["Originals"] or []), "fence": flight["Fence"],
                        "end_relative_ns": flight["EndNS"], "end_ack": flight["EndAck"],
                        "delivered": sample["Bytes"], "rto_count": sample["RTOs"],
                        "retransmits": sample["Retransmits"], "sack_recoveries": sample["SACKRecoveries"],
                        "tlp_recoveries": sample["TLPRecoveries"], "first_choice": acts[:1],
                        "action_counts": dict(Counter(e["Action"]["Path"] for e in acts)),
                        "first_retransmits": post[:8], "holes": repairs, "holes_sha256": digest(holes),
                        "split_index": flight["SplitIndex"], "expected": flight.get("Expected"),
                        "actual": flight.get("Actual"), "max_external_stage_ns": flight["MaxProtectedWaitNS"],
                        "staged_packets": flight["StagedPackets"], "latched_mode": flight["SenderLatchedMode"],
                        "state_at_end": last}
    if ((arms["reference"]["Flight"]["Match"] == "reference") != ("rack_replay" in arms) or
            ("rack_replay" in arms and
             ((arms["rack_replay"]["Flight"]["Match"] == "reference_prefix_matched") != ("sack_replay" in arms)))):
        raise ValueError("Replay continued after an unmatched reference or skipped a matched arm")
    if roles == set(ROLES) and output["sack_replay"]["match"] in ("matched_decision", "no_observed_mode_divergence"):
        rack, sack = (arms[role]["Flight"] for role in ROLES[1:])
        horizon = min(rack["EndNS"], sack["EndNS"])
        contrast = {}
        for side, label in enumerate(("forward", "reverse")):
            series = [[e["Wire"] for e in arm["Events"] if e["Kind"] == "offer"
                       and e["Wire"]["Side"] == side and e["At"] <= horizon] for arm in (rack, sack)]
            split = next((i for i, (left, right) in enumerate(zip(*series)) if left != right), min(map(len, series)))
            contrast[label] = {"equal_offers": split,
                               "rack_next": series[0][split] if split < len(series[0]) else None,
                               "sack_next": series[1][split] if split < len(series[1]) else None}
        common = {}
        for role in ROLES[1:]:
            item = output[role]
            common[role] = {"holes_repaired": sum(h["repair_ns"] is not None and h["repair_ns"] <= horizon
                                                   for h in item["holes"]),
                            "holes_cumulatively_acked": sum(h["cumulative_ack_ns"] is not None
                                                            and h["cumulative_ack_ns"] <= horizon for h in item["holes"]),
                            "rtos": sum(e["At"] <= horizon and e.get("Action", {}).get("Path") == "rto"
                                        for e in arms[role]["Flight"]["Events"])}
        output["comparison"] = {"common_horizon_ns": horizon, "first_unequal_offers": contrast, "common": common}
    return output


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
    owner = OWNER
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
    xml_runs = {}
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
            if (len(names) != len(set(names)) or ROOT not in names or any(list(case) for case in cases)
                    or run in xml_runs):
                raise ValueError(f"{phase}: unexpected XML cases {names}")
            xml_runs[run] = set(names) - {ROOT}
        elif row["name"] == "test.log":
            parts = {}
            expected = {}
            for line in body.decode().splitlines():
                if "VIRTUAL_TCP_PART " not in line:
                    continue
                name, ordinal, chunk = line.split("VIRTUAL_TCP_PART ", 1)[1].split(" ", 2)
                index, count = map(int, ordinal.split("/"))
                rows = parts.setdefault(name, {})
                if not 0 <= index < count or index in rows or count != expected.setdefault(name, count):
                    raise ValueError(f"{phase}: duplicate or malformed native trace chunks")
                rows[index] = chunk
            values = []
            for name, rows in parts.items():
                value = json.loads("".join(rows[index] for index in range(expected[name])))
                if name != value["Case"]:
                    raise ValueError(f"{phase}: native trace chunk identity differs")
                values.append(value)
            cases = {value["Case"]: value for value in values}
            if len(cases) != len(values) or run in samples or any(not name.startswith(ROOT + "/") for name in cases):
                raise ValueError(f"{phase}: invalid virtual transfer identities")
            samples[run] = cases
        else:
            raise ValueError(f"{phase}: unexpected report {row['name']}")
        references.append(row)
    if set(samples) != set(range(1, runs + 1)) or set(xml_runs) != set(samples):
        raise ValueError(f"{phase}: log/XML census differs")
    baseline = samples[1]
    for run, sample in samples.items():
        if xml_runs[run] != set(sample) or sample != baseline:
            raise ValueError(f"{phase}: case identity or complete virtual observations differ in run {run}")
    return {"attempts": runs, "measurements": measure(baseline), "reports": references,
            "complete_observation_sha256": digest(baseline)}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [OWNER, OWNER + "_nogo", TCP_NOGO]:
        raise ValueError("Unexpected recovery qualification owners")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; '
                           'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
    formatted = invoke("gofmt", ["run", *options, "--run_under=" + formatter,
                                "@io_bazel_rules_go//go:go"], "command", targets=[])
    if formatted["status"] != "PASS":
        raise RuntimeError("Canonical Go formatting failed")
    selected = ["--strategy=TestRunner=remote", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=errors", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1"]
    result = {"status": "INCOMPLETE", "source": config["source"], "phases": {}}
    try:
        for phase, count, extra in (("virtual-normal", 2, []), ("virtual-race", 1, ["--config=race"])):
            checked = invoke(phase, ["test", *options, *extra, *selected,
                                     f"--runs_per_test={count}", f"--test_arg=-test.run=^{ROOT}$", OWNER],
                             "test", targets=[OWNER])
            if checked["status"] != "PASS":
                raise RuntimeError(f"{phase}: native recovery owner did not pass")
            result["phases"][phase] = observations(evidence, phase, count)
        if result["phases"]["virtual-normal"]["complete_observation_sha256"] != result["phases"]["virtual-race"]["complete_observation_sha256"]:
            raise ValueError("Normal and race virtual packet observations differ")
        for phase, analyzer in (("virtual-nogo", OWNER + "_nogo"), ("tcp-nogo", TCP_NOGO)):
            analyzed = invoke(phase, ["test", *options, *selected, "--runs_per_test=1", analyzer],
                              "test", targets=[analyzer])
            if analyzed["status"] != "PASS":
                raise RuntimeError(f"{phase}: analyzer owner did not pass")
            native_results(evidence / f"invocation-{phase}", phase, analyzer, 1)
        result["status"] = "PASS"
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        (evidence / "virtual-tcp-determinism.json").write_text(json.dumps(result, indent=2) + "\n")
