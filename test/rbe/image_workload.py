"""Compare approved recovery changes on the original continuously dropping FIFO."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
ROOT = "TestVirtualTCPOriginalFIFORTO"
ROLE = "original_fifo"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def summarize(s: dict, plus: bool) -> dict:
    f, q, startup = s["Flight"], s["ForwardQueue"], s["Startup"]
    total, mss = 16 << 20, 1420
    if (s["Algorithm"] != "cubic" or s["OfferedBytes"] != total or startup["MSS"] != mss
            or not startup["ACKs"] or any(ack["Plus"] != plus for ack in startup["ACKs"])
            or startup["FirstPayloadNS"] != 150_094_400 or s["OneWayDelayNS"] != 50_000_000
            or s["BDPBytes"] != 125_000 or q["Rate"] != 1_250_000 or q["Capacity"] != 125_000
            or q["OfferedPackets"] != q["AdmittedPackets"] + q["DroppedPackets"]
            or q["OfferedBytes"] != q["AdmittedBytes"] + q["DroppedBytes"]
            or f["Role"] != ROLE or f["SenderMode"] != 1 or f["ReceiverMode"] != 1
            or f["SenderLatchedMode"] != 1 or f["StagedPackets"] or f["MaxProtectedWaitNS"]
            or f["EndNS"] != s["VirtualNS"] - startup["FirstPayloadNS"]):
        raise ValueError("CUBIC startup, original finite FIFO or recovery mode changed")
    events = f["Events"]
    peers = [e["Endpoint"] for e in events if "Endpoint" in e]
    if ({p["Side"] for p in peers} != {0, 1} or not all(p["SACKEnabled"] and p["Timestamps"] for p in peers)
            or max(e["At"] for e in events) > 2_100_000_000):
        raise ValueError("Bounded trace is missing real negotiated endpoint observations")
    offers = [e["Wire"] for e in events if e["Kind"] == "offer"]
    drops = [w for w in offers if w["Dropped"]]
    discards = [e["Wire"] for e in events if e["Kind"] == "discard"]
    if Counter(map(digest, drops)) != Counter(map(digest, discards)):
        raise ValueError("Observed FIFO refusals do not match actual discard events")
    if plus and (len(offers) < 544 or digest(offers[:544]) != "afc3edfdd333e918f8f91ba5846cf3ba2734a2a246c15898a9a3067cf0765713"):
        raise ValueError("The original offers through the first FIFO refusal changed")
    accepted, delivered = s["AcceptedBytes"], s["Bytes"]
    if not 0 <= delivered <= accepted <= total or not 0 <= f["EndAck"] <= accepted:
        raise ValueError("Delivered or cumulatively ACKed beyond accepted payload")
    if delivered == total and s["PayloadSHA256"] != "23d905a2e230c690a35b2337aa115ffd55d01e485c1cfa57548d12deccb464a4":
        raise ValueError("Full payload hash differs from the original transfer")
    match f["End"]:
        case "payload_fin_drained":
            if (s["RecoveryClass"] != "finite_fifo" or f["EndAck"] != total or delivered != total
                    or not f["EOF"] or not f["FINAcknowledged"] or f["PendingPackets"]
                    or s["VirtualNS"] > 30_001_000_000):
                raise ValueError("Invalid complete payload/FIN drain")
        case "original_deadline_incomplete":
            if (s["RecoveryClass"] != f["End"] or not 30_000_000_000 <= s["VirtualNS"] <= 30_001_000_000
                    or (f["EOF"] and f["FINAcknowledged"] and not f["PendingPackets"])):
                raise ValueError("Invalid original deadline readout")
        case other:
            raise ValueError(f"Unexpected end of the original FIFO: {other}")
    rto = f.get("FirstRTO")
    first_stat = next((r["AtNS"] - startup["FirstPayloadNS"] for r in s["RecoveryTrace"] if r["RTOs"]), None)
    if (bool(s["RTOs"]) != (rto is not None) or bool(s["RTOs"]) != (first_stat is not None)
            or (rto is not None and (rto["Action"]["Path"] != "rto" or not 0 <= first_stat - rto["At"] <= 1_000_000))):
        raise ValueError("First real sender RTO callback does not match its counter")
    watch = 651 * mss
    return {"source_case": s["Case"], "duration_from_data_ns": f["EndNS"], "clock_ns": s["VirtualNS"],
            "outcome": f["End"], "accepted": accepted, "delivered": delivered, "acked": f["EndAck"],
            "eof": f["EOF"], "fin_acknowledged": f["FINAcknowledged"], "pending": f["PendingPackets"],
            "payload_sha256": s["PayloadSHA256"], "wire_sha256": s["AllPacketsSHA256"], "packets": s["PacketCount"],
            "retransmits": s["Retransmits"], "rtos": s["RTOs"], "sack_recoveries": s["SACKRecoveries"],
            "tlp_recoveries": s["TLPRecoveries"], "queue": q, "first_drop": drops[0] if drops else None,
            "first_rto": rto, "first_rto_stat_ns": first_stat,
            "watch_offers": [w for w in offers if not w["Side"] and w["Seq"] <= watch < w["End"]],
            "watch_first_ack": next((e["Wire"] for e in events if e["Kind"] == "input" and e["Wire"]["Side"]
                                     and e["Wire"]["Ack"] > watch), None),
            "detailed_dropped_retries": sum(w["Retransmit"] for w in drops)}


def measure(samples: dict) -> dict:
    if set(samples) != {f"{ROOT}/classic", f"{ROOT}/plus"}:
        raise ValueError("Expected the original FIFO under both startup choices")
    return {arm: summarize(samples[f"{ROOT}/{arm}"], arm == "plus") for arm in ("classic", "plus")}


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
    source, child, prerequisite = config["source"], config["product"], config["prerequisite"]
    if (config["targets"] != [OWNER] or config["expected_sources"] != [source, child, prerequisite]
            or subprocess.check_output(["git", "rev-parse", source + "^", source + "^^"], text=True).splitlines() != [child, prerequisite]):
        raise ValueError("Unexpected native owner or candidate ancestry")
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
                "--test_env=GO_TEST_WRAP_TESTV=1", "--runs_per_test=1", f"--test_arg=-test.run=^{ROOT}$"]
    result = {"status": "INCOMPLETE", "source": source, "phases": {}}
    try:
        for phase, revision in (("sack-credit", prerequisite), ("rack-flight", child)):
            subprocess.run(["git", "switch", "--detach", revision], check=True)
            checked = invoke(phase, ["test", *options, *selected, OWNER], "test", targets=[OWNER], source=revision)
            if checked["source"] != revision:
                raise RuntimeError(f"{phase}: native source differs from the requested candidate")
            if checked["status"] != "PASS":
                result["phases"][phase] = {"source": revision, "error": "native finite FIFO owner failed"}
                continue
            try:
                result["phases"][phase] = {"source": revision, **observations(evidence, phase, 1)}
            except (KeyError, ValueError, OSError) as error:
                result["phases"][phase] = {"source": revision, "error": str(error)}
        if any("error" in phase for phase in result["phases"].values()):
            raise RuntimeError("One or more candidate observations failed")
        result["status"] = "PASS"
    finally:
        subprocess.run(["git", "switch", "--detach", source], check=True)
        (evidence / "virtual-tcp-determinism.json").write_text(json.dumps(result, indent=2) + "\n")
