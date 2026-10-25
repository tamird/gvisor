"""Qualify the issue 16393 guard predicates in a fixed no-timestamp TCP model."""
import hashlib
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
ROOT = "TestVirtualTCPNoTimestampBurst"
SCHEDULES = {"none": (0, 0), "blackout": (1, 11), "partial": (2, 8)}
BODY = bytes((i * 17 + i // 251) % 256 for i in range(512 << 10))
RTO_RECOVERY = 1
SACK_RECOVERY = 3


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def ahead(seq: int, base: int) -> bool:
    return 0 < (seq - base) % 2**32 < 2**31


def trigger(event: dict, guard: str) -> bool:
    s = event.get("Sender")
    if not s or s["State"] != RTO_RECOVERY:
        return False
    if guard == "ack":
        return s["Kind"] == "ack_scan" and s["Lost"] > 0 and not s["Active"]
    return s["Kind"] == "timer_expired"


def sparse(ack: int, blocks: list | None) -> bool:
    return any(ahead(b["Start"], ack) and ahead(b["End"], b["Start"]) for b in blocks or [])


def summarize(sample: dict, algorithm: str, schedule: str, guards: tuple[bool, bool]) -> dict:
    s = sample
    first, last = SCHEDULES[schedule]
    if ((s["Algorithm"], s["KernelOS"], s["KernelArch"], s["Go"]) != (algorithm, "linux", "amd64", "go1.27.3")
            or s["Schedule"] != {"Name": schedule, "First": first, "Last": last}
            or (s["GuardACK"], s["GuardTimer"]) != guards or s["Offered"] != len(BODY) or s["Accepted"] != len(BODY)
            or not 0 <= s["Delivered"] <= len(BODY) or not 0 <= s["Acknowledged"] <= len(BODY)
            or s["BodyHash"] != hashlib.sha256(BODY[:s["Delivered"]]).hexdigest()):
        raise ValueError(f"{s['Case']}: incorrect transport, workload, or delivered prefix")
    events = s["Events"]
    wire = [entry["Wire"] for entry in events if "Wire" in entry]
    states = [entry["Sender"] for entry in events if "Sender" in entry]
    syn = [(w["From"], w["Flags"], w["Permitted"]) for w in wire if w["Flags"] & 2]
    if syn != [(0, 2, True), (1, 18, True)] or any(w["TS"] for w in wire) or any(
            r["Timestamps"] or not r["SACKEnabled"] for r in states):
        raise ValueError(f"{s['Case']}: not actual SACK/no-timestamp negotiation")
    offers = [w for w in wire if w["From"] == 0 and w["Bytes"]]
    drops = [w for w in wire if w["Dropped"]]
    if ([w["Offer"] for w in offers] != list(range(1, len(offers) + 1)) or s["Offers"] != len(offers)
            or offers[0]["At"] != s["FirstData"] or len(drops) != s["Drops"]
            or [w["Offer"] for w in drops] != (list(range(first, last + 1)) if first else [])):
        raise ValueError(f"{s['Case']}: actual physical drop schedule changed")
    elapsed = s["End"] - s["FirstData"]
    completed = s["EOF"] and s["FinACK"] and not s["Pending"]
    if (not 0 <= elapsed <= 35_000_000_000
            or completed and (s["Acknowledged"] != len(BODY) or s["Delivered"] != len(BODY))
            or not completed and elapsed != 35_000_000_000
            or schedule == "none" and (not completed or s["RTOs"] or s["Retransmissions"])):
        raise ValueError(f"{s['Case']}: inconsistent virtual endpoint accounting")
    sack = [w for w in wire if w["From"] == 1 and sparse(w["ACK"], w["SACK"])]
    if schedule == "partial" and not sack:
        raise ValueError(f"{s['Case']}: partial-flight schedule did not produce a real sparse SACK")
    rtos = [r for r in states if r["Kind"] == "rto_interrupt"]
    resets = [r for r in states if r["Kind"] == "rto_enter"]
    if len(resets) != len(rtos) or len(rtos) != s["RTOs"] or schedule == "blackout" and not rtos:
        raise ValueError(f"{s['Case']}: real timeout path is missing")
    for start, end in zip(rtos, resets):
        if (start["At"] != end["At"] or start["NXT"] != end["NXT"]
                or end["Last"] != (start["NXT"] - 1) % 2**32 or end["State"] != RTO_RECOVERY or end["Active"]):
            raise ValueError(f"{s['Case']}: timeout interruption and boundary disagree")
    reached = {g: [i for i, event in enumerate(events) if trigger(event, g)] for g in ("ack", "timer")}
    if (bool(reached["ack"]), bool(reached["timer"])) != (s["EligibleACK"], s["EligibleTimer"]):
        raise ValueError(f"{s['Case']}: guard enrollment differs from actual sender events")
    # RFC 6675 5.1 applies when RTO interrupted SACK recovery. Track coverage
    # of that saved point independently of any later enum or FR.Last overwrite.
    pending_point = None
    interrupted = []
    before_point = []
    for event in states:
        if pending_point is not None:
            covering_ack = (event["Kind"] == "ack_begin" and ahead(event["ACK"], pending_point)
                            and not ahead(event["UNA"], event["ACK"]) and not ahead(event["ACK"], event["NXT"]))
            if ahead(event["UNA"], pending_point) or covering_ack:
                pending_point = None
        if event["Kind"] == "rto_interrupt" and event["Active"] and event["State"] == SACK_RECOVERY:
            pending_point = (event["NXT"] - 1) % 2**32
            interrupted.append({"at": event["At"], "last": event["Last"], "una": event["UNA"], "point": pending_point})
        if pending_point is not None and event["Kind"] == "enter_recovery":
            before_point.append({"at": event["At"], "state": event["State"], "una": event["UNA"], "point": pending_point})
    sparse_at_rto = [r for r in states if r["Kind"] == "ack_begin" and r["State"] == RTO_RECOVERY
                     and sparse(r["ACK"], r["SACK"])]
    return {"algorithm": algorithm, "schedule": schedule, "guards": guards, "completed": completed,
            "delivered": s["Delivered"], "acknowledged": s["Acknowledged"], "pending": s["Pending"],
            "eof": s["EOF"], "fin_ack": s["FinACK"], "elapsed_ns": elapsed, "original_bytes_hash": s["BodyHash"],
            "drops": drops, "retransmissions": s["Retransmissions"], "rtos": s["RTOs"],
            "sack_recoveries": s["SACKRecoveries"], "real_sparse_sacks": len(sack),
            "sparse_sacks_on_rto_ingress": len(sparse_at_rto), "eligible": {g: len(v) for g, v in reached.items()},
            "first_trigger": {g: events[v[0]]["Sender"] if v else None for g, v in reached.items()},
            "timeout_interrupts_sack_recovery": interrupted, "reentry_before_covered": before_point,
            "wire_sha256": digest(wire), "events_sha256": digest(events)}


def measure(samples: dict) -> dict:
    cases = {name.removeprefix(ROOT + "/"): s for name, s in samples.items()}
    expected = {}
    for schedule in SCHEDULES:
        for algorithm in ("reno", "cubic"):
            label = f"{algorithm}_{schedule}"
            expected[label] = (algorithm, schedule, (False, False))
        base = cases["reno_" + schedule]["Events"]
        ack, timer = (any(trigger(entry, guard) for entry in base) for guard in ("ack", "timer"))
        for suffix, enabled in (("ack", (True, False)), ("timer", (False, True)), ("both", (True, True))):
            if (not enabled[0] or ack) and (not enabled[1] or timer):
                expected[f"reno_{schedule}_{suffix}"] = ("reno", schedule, enabled)
    if set(expected) != set(cases):
        raise ValueError(f"Baseline-based treatment census differs: {set(expected) ^ set(cases)}")
    summary = {label: summarize(cases[label], *args) for label, args in expected.items()}
    for label, (_, schedule, guards) in expected.items():
        if not any(guards):
            continue
        ref = cases["reno_" + schedule]["Events"]
        arm = cases[label]["Events"]
        gate = next(i for i, e in enumerate(ref) if any(en and trigger(e, g) for en, g in zip(guards, ("ack", "timer"))))
        if arm[:gate + 1] != ref[:gate + 1]:
            raise ValueError(f"{label}: wire or sender history diverged before the first selected guard")
        ref_wire = [entry["Wire"] for entry in ref if "Wire" in entry]
        arm_wire = [entry["Wire"] for entry in arm if "Wire" in entry]
        i = next((i for i, pair in enumerate(zip(ref_wire, arm_wire)) if pair[0] != pair[1]), min(len(ref_wire), len(arm_wire)))
        summary[label]["matched_events_through_trigger"] = gate + 1
        summary[label]["matched_wire_offers"] = i
        summary[label]["first_different_wire_offer"] = {"stock": ref_wire[i] if i < len(ref_wire) else None,
                                                        "guard": arm_wire[i] if i < len(arm_wire) else None}
    return summary


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
    source = config["source"]
    if config["targets"] != [OWNER] or config["expected_sources"] != [source]:
        raise ValueError("Unexpected source or native owner")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"], "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]
    # This revision's formatter is goimports -format-only. Use Bazel's pinned
    # target directly so it inherits the qualification's authenticated config.
    changed_go = ["pkg/tcpip/tests/integration/reno_burst_test.go",
                  *(f"pkg/tcpip/transport/tcp/{name}.go" for name in ("connect", "protocol", "rack", "snd"))]
    formatted = invoke("goimports", ["run", *options, "--run_in_cwd", "@org_golang_x_tools//cmd/goimports", "--",
                                     "-format-only", "-d", *changed_go], "command", targets=[])
    if formatted["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").stat().st_size:
        raise RuntimeError("Canonical goimports check failed")
    selected = ["--strategy=TestRunner=remote", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=errors", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1", "--runs_per_test=2", f"--test_arg=-test.run=^{ROOT}$"]
    checked = invoke("no-timestamps", ["test", *options, *selected, OWNER], "test", targets=[OWNER])
    if checked["source"] != source or checked["status"] != "PASS":
        raise RuntimeError("Native no-timestamp fixture failed")
    result = observations(evidence, "no-timestamps", 2)
    (evidence / "reno-no-timestamps.json").write_text(json.dumps(result, indent=2) + "\n")
