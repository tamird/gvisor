"""Retain both aged-path TCP arms before interpreting native observations."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
ANALYZER = OWNER + "_nogo"
CASES = {f"TestVirtualTCPAgedPath/{algorithm}/{order}"
         for algorithm in ("reno", "cubic") for order in ("in_order", "reordered")}
PARENTS = {"TestVirtualTCPAgedPath", "TestVirtualTCPAgedPath/reno", "TestVirtualTCPAgedPath/cubic"}


def check_phase(evidence: Path, phase: dict, paths: set[Path]) -> dict:
    """Join native source/configuration/run identities to immutable log/XML."""
    directory = evidence / ("invocation-" + phase["name"])
    result = phase["result"]
    context = json.loads((directory / "context.json").read_text())
    if (result["source"] != phase["source"] or context["expected_source"] != phase["source"]
            or (directory / "head.txt").read_text().strip() != phase["source"]):
        raise ValueError(f"{phase['name']}: native source identity differs")
    if result["status"] != "PASS" or result["native_exit"] != 0:
        raise ValueError(f"{phase['name']}: native phase did not pass")
    if phase["kind"] == "format":
        if (directory / "stdout.log").read_bytes():
            raise ValueError(f"{phase['name']}: goimports reported a difference")
        return {"status": "PASS"}
    match result["executed"]:
        case [attempt]:
            pass
        case _:
            raise ValueError(f"{phase['name']}: expected exactly one executed owner")
    if (attempt["owner"]["label"] != phase["owner"] or attempt["status"] != "PASSED"
            or (attempt["run"], attempt["attempt"], attempt["shard"]) != (1, 1, 1)
            or attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False)
            or attempt["execution_info"].get("strategy") != "local"):
        raise ValueError(f"{phase['name']}: expected one uncached coordinator execution")
    expected_key = (attempt["owner"]["label"], attempt["owner"]["configuration"], 1, 1, 1)
    manifest = json.loads(report.manifest_path(directory / "events.jsonl").read_text())
    _, _, errors = report.retained_reports(report.manifest_path(directory / "events.jsonl"))
    if errors:
        raise ValueError(f"{phase['name']}: invalid retained reports: {errors}")
    bodies = {}
    for row in manifest["reports"]:
        identity = row["identity"]
        key = (identity["label"], identity["configuration"]["id"], identity["run"],
               identity["attempt"], identity["shard"])
        path = (directory / row["path"]).resolve()
        path.relative_to(directory.resolve())
        if key != expected_key or path in paths or row["name"] in bodies:
            raise ValueError(f"{phase['name']}: repeated or unmatched report identity/path")
        paths.add(path)
        body = path.read_bytes()
        if report.file_record(path) != {"bytes": row["bytes"], "sha256": row["sha256"]}:
            raise ValueError(f"{phase['name']}: retained report identity differs")
        bodies[row["name"]] = body
    if set(bodies) != {"test.log", "test.xml"}:
        raise ValueError(f"{phase['name']}: expected exactly log and XML")
    xml = list(ET.fromstring(bodies["test.xml"]).iter("testcase"))
    names = [case.attrib["name"] for case in xml]
    if not names or len(names) != len(set(names)) or any(list(case) for case in xml):
        raise ValueError(f"{phase['name']}: invalid positive XML census")
    log = bodies["test.log"].decode()
    if phase["kind"] == "nogo":
        if len(names) != 1 or not all(f"{cpu}...PASS" in log for cpu in ("amd64", "arm64")):
            raise ValueError(f"{phase['name']}: missing actual analyzer CPU results")
        return {"names": names, "reports": manifest["reports"]}
    if set(names) != CASES | PARENTS:
        raise ValueError(f"{phase['name']}: unexpected XML names: {names}")
    values = [json.loads(line.split("VIRTUAL_TCP ", 1)[1])
              for line in log.splitlines() if "VIRTUAL_TCP " in line]
    if len(values) != len(CASES) or {value["Case"] for value in values} != CASES:
        raise ValueError(f"{phase['name']}: incomplete or duplicate observation census")
    if any(value["Bytes"] != 65536 or value["ProbeOverflow"] for value in values):
        raise ValueError(f"{phase['name']}: incomplete transfer/probe evidence")
    return {"names": names, "samples": {value["Case"]: value for value in values},
            "reports": manifest["reports"]}


def check_link(value: dict) -> dict:
    """Verify the recorded schedule and bounded flight without assuming a winner."""
    name = value["Case"]
    algorithm, ordering = name.split("/")[-2:]
    expected_payload = bytes((i * 17 + i // 251) % 256 for i in range(65536))
    if (value["Algorithm"] != algorithm or value["PayloadSHA256"] != hashlib.sha256(expected_payload).hexdigest()
            or any(value[key] != 65536 for key in ("Bytes", "OfferedBytes", "AcceptedBytes"))
            or value["DropOffset"] != -1 or value["Holes"] or value["ProbeOverflow"]
            or value["RateBytesPerSecond"] != 2500000):
        raise ValueError(f"{name}: wrong workload or incomplete payload/probe evidence")
    phases = value["PathPhases"]
    if [phase["Name"] for phase in phases] != ["short_path", "idle", "long_path", "bounded_flight"]:
        raise ValueError(f"{name}: wrong phase census")
    short, idle, high, flight = phases
    if (idle["EndNS"] - idle["BeginNS"] != 310_000_000_000
            or short["EndNS"] != idle["BeginNS"] or idle["EndNS"] != high["BeginNS"]
            or high["EndNS"] != flight["BeginNS"]):
        raise ValueError(f"{name}: path/idle timeline differs")
    mss = high["Sample"]["MSS"]
    if not 0 < mss <= 1460:
        raise ValueError(f"{name}: invalid actual MSS {mss}")
    for phase, count in ((short, 16384), (idle, 16384), (high, 32768), (flight, 32768 + 3*mss)):
        if any(phase["Sample"][key] != count for key in
               ("AcceptedBytes", "DeliveredBytes", "CumulativelyAcknowledgedBytes")):
            raise ValueError(f"{name}: phase ended without actual delivery and ACK: {phase['Name']}")
    final = value["Rounds"][-1]
    if (final["DeliveredBytes"] != 65536 or final["CumulativelyAcknowledgedBytes"] != 65536
            or final["Probe"]["SndUna"] != final["Probe"]["SndNxt"]):
        raise ValueError(f"{name}: missing final cumulative ACK/FIN completion")
    trace = value["Trace"]
    encode = lambda row: json.dumps(row, separators=(",", ":")).encode()
    if (hashlib.sha256(encode(trace)).hexdigest() != value["TraceSHA256"]
            or hashlib.sha256(b"".join(encode(row) + b"\n" for row in trace)).hexdigest() != value["AllPacketsSHA256"]
            or len(trace) != value["PacketCount"] or len(value["DeliveredTrace"]) != len(trace)
            or sorted(row["CapturedOrder"] for row in trace) != list(range(1, len(trace)+1))):
        raise ValueError(f"{name}: incomplete full packet census/hash")
    lag = 10_000_000 if ordering == "reordered" else 0
    if value["ReorderDelayNS"] != lag or not value["ReorderSelected"]:
        raise ValueError(f"{name}: wrong single-segment delay treatment")
    selected = [row for row in trace if row["From"] == 0 and row["PayloadBytes"]
                and not row["Retransmitted"] and row["Sequence"] == value["ReorderSequence"]]
    if len(selected) != 1:
        raise ValueError(f"{name}: ambiguous reordered original segment")
    selected = selected[0]
    next_tx = [0, 0]
    for row in sorted(trace, key=lambda row: row["CapturedOrder"]):
        side = row["From"]
        if side not in (0, 1) or row["Dropped"] or row["PacketBytes"] < row["PayloadBytes"] + 40:
            raise ValueError(f"{name}: invalid real link packet")
        serial = (row["PacketBytes"] * 1_000_000_000 + 2499999) // 2500000
        next_tx[side] = max(next_tx[side], row["CapturedAtNS"]) + serial
        propagation = 5_000_000 if row["CapturedAtNS"] < idle["BeginNS"] else 50_000_000
        extra = lag if row["CapturedOrder"] == selected["CapturedOrder"] else 0
        if (row["SerializationEndNS"] != next_tx[side] or row["PropagationNS"] != propagation
                or row["AtNS"] != next_tx[side] + propagation + extra):
            raise ValueError(f"{name}: actual packet scheduling differs at {row['CapturedOrder']}")
    for original, delivered in zip(trace, value["DeliveredTrace"], strict=True):
        normalized = dict(delivered, DeliveredAtNS=0)
        if normalized != original or not 0 <= delivered["DeliveredAtNS"] - original["AtNS"] <= 1_000_000:
            raise ValueError(f"{name}: missing or late actual packet delivery")
    originals = [row for row in trace if row["From"] == 0 and row["PayloadBytes"]
                 and not row["Retransmitted"] and flight["BeginNS"] <= row["CapturedAtNS"] < flight["EndNS"]]
    originals.sort(key=lambda row: row["Sequence"])
    if (len(originals) != 3 or originals[0] != selected
            or any(row["PayloadBytes"] != mss for row in originals)
            or [row["Sequence"] for row in originals] != [selected["Sequence"] + i*mss for i in range(3)]):
        raise ValueError(f"{name}: bounded flight is not exactly three contiguous MSS")
    delivered = {row["CapturedOrder"]: row for row in value["DeliveredTrace"]}
    selected_at = delivered[selected["CapturedOrder"]]["DeliveredAtNS"]
    higher_before = [row for row in originals[1:] if delivered[row["CapturedOrder"]]["DeliveredAtNS"] < selected_at]
    if len(higher_before) != (2 if lag else 0):
        raise ValueError(f"{name}: actual receiver did not observe the intended ordering")
    syns = [row for row in trace if row["Flags"] & 2]
    if len(syns) != 2 or {row["From"] for row in syns} != {0, 1} or not all(row["SACKPermitted"] for row in syns):
        raise ValueError(f"{name}: real SACK negotiation missing")
    counters = ("Retransmits", "RTOs", "SACKRecoveries", "TLPRecoveries")
    deltas = {key: flight["Sample"][key] - high["Sample"][key] for key in counters}
    if any(count < 0 for count in deltas.values()) or (not lag and any(deltas.values())):
        raise ValueError(f"{name}: invalid scoped recovery counters {deltas}")
    probes = [row for row in value["Probes"] if flight["BeginNS"] <= row["AtNS"] <= flight["EndNS"]]
    if not probes:
        raise ValueError(f"{name}: missing measured post-ACK TCP snapshots")
    return {"algorithm": algorithm, "ordering": ordering, "delivered_bytes": value["Bytes"],
            "bounded_flight_bytes": 3*mss, "bounded_flight_ns": flight["EndNS"]-flight["BeginNS"],
            "recovery_deltas": deltas, "warmup_counters": {key: high["Sample"][key] for key in counters},
            "before_flight": high["Sample"], "after_flight": flight["Sample"],
            "flight_probes": probes, "selected_segment": selected,
            "higher_segments_delivered_first": len(higher_before), "full_packet_count": len(trace),
            "scope": "Finite virtual-time recovery; warmup is separate; no native throughput or Linux fairness claim."}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    arms = config["arms"]
    if (config["targets"] != [OWNER, ANALYZER]
            or [arm["name"] for arm in arms] != ["before", "after"]
            or report.source_set(config["expected_sources"]) != {arm["source"] for arm in arms}
            or config["source"] != arms[1]["source"]):
        raise ValueError("Unexpected two-arm source/owner contract")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    race_options = [value for value in options if value != "--lockfile_mode=error"]
    prepared = invoke("race-dependency-preparation", ["cquery", *race_options, "--config=race",
                      "--lockfile_mode=" + config.get("preparation_lock_mode", "update"),
                      "--output=label", "set(" + OWNER + ")"], "command", targets=[])
    if prepared["status"] != "PASS":
        raise RuntimeError("Race configured dependency preparation failed")
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
                "--flaky_test_attempts=1", "--test_output=all", "--test_timeout=300",
                "--test_sharding_strategy=disabled", "--test_tag_filters=", "--keep_going"]
    record = {"status": "INCOMPLETE", "source": config["source"],
              "expected_sources": config["expected_sources"], "arms": [], "restored": False,
              "scope": "Two real-stack aged-path arms; native identity, exact schedule/payload, within-arm normal/race equality and scoped recovery deltas. No native throughput or Linux fairness claim."}
    try:
        for arm in arms:
            source = arm["source"]
            subprocess.run(["git", "checkout", "--detach", source], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Source switch did not reach a clean arm")
            for name, expected in arm["matched_inputs"].items():
                path = Path(name)
                body = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
                if hashlib.sha256(body).hexdigest() != expected:
                    raise ValueError(f"{arm['name']}: source input differs: {name}")
            current = {"name": arm["name"], "source": source, "product": arm["product"], "phases": []}
            record["arms"].append(current)
            name = arm["name"] + "-goimports"
            formatted = invoke(name, ["run", *options, "--run_in_cwd", "@org_golang_x_tools//cmd/goimports",
                                     "--", "-format-only", "-d", "pkg/tcpip/tests/integration/virtual_tcp_test.go"],
                               "command", targets=[], source=source)
            current["phases"].append({"name": name, "source": source, "kind": "format", "result": formatted})
            for suffix, owner, extra in (("normal-1", OWNER, []), ("normal-2", OWNER, []),
                                         ("race", OWNER, ["--config=race"]), ("nogo", ANALYZER, [])):
                name = arm["name"] + "-" + suffix
                args = ["test", *options, *extra, *selected]
                if owner == OWNER:
                    args.extend(["--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1",
                                 "--test_arg=-test.run=^TestVirtualTCPAgedPath$"])
                result = invoke(name, [*args, owner], "test", targets=[owner], source=source)
                current["phases"].append({"name": name, "source": source, "kind": suffix,
                                          "owner": owner, "result": result})
        # A native failure must not hide the remaining arm's native evidence.
        observed_sources = {path.parent.name.removeprefix("invocation-"):
                            json.loads(path.read_text())["source"]
                            for path in evidence.glob("invocation-*/verdict.json")}
        if observed_sources != config["expected_phase_sources"]:
            raise ValueError("Actual phase/source mapping differs from the frozen plan")
        paths = set()
        for arm in record["arms"]:
            for phase in arm["phases"]:
                phase["checked"] = check_phase(evidence, phase, paths)
            samples = [phase["checked"]["samples"] for phase in arm["phases"]
                       if phase["kind"] in ("normal-1", "normal-2", "race")]
            if not all(sample == samples[0] for sample in samples[1:]):
                raise ValueError(f"{arm['name']}: complete normal/race observations differ")
            arm["observations"] = {name: check_link(value) for name, value in samples[0].items()}
        record["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(status="FAIL", error=str(error))
        raise
    finally:
        try:
            subprocess.run(["git", "checkout", "--detach", config["source"]], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != config["source"]
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Failed to restore initial qualification source")
            record["restored"] = True
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record.update(status="FAIL", restoration_error=str(error))
            raise
        finally:
            report.write_json(evidence / "virtual-tcp-pair.json", record)
