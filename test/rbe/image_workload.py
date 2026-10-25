"""Compare one ACK-earned repair against its authenticated physical-FIFO control."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
TCP_NOGO = "//pkg/tcpip/transport/tcp:tcp_test_nogo"
ROOT = "TestVirtualTCPOriginalFIFORTO"
ROLE = "original_fifo"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def covered(intervals: list[tuple[int, int]], first: int = 0) -> int:
    edge = first
    for left, right in sorted(intervals):
        if left > edge:
            break
        edge = max(edge, right)
    return edge


def measure_original(samples: dict) -> dict:
    key = f"{ROOT}/{ROLE}"
    if set(samples) != {key}:
        raise ValueError("Expected exactly the original finite-FIFO diagnostic")
    s = samples[key]
    f = s["Flight"]
    first, base = s["Startup"]["FirstPayloadNS"], s["Startup"]["DataSequence"]
    q = s["ForwardQueue"]
    if (s["VirtualNS"] != 30_000_331_840 or s["Bytes"] != 1_121_800 or s["AcceptedBytes"] != 9_510_408
            or s["OfferedBytes"] != 16 << 20 or s["Algorithm"] != "cubic" or s["Startup"]["MSS"] != 1420
            or not all(ack["Plus"] for ack in s["Startup"]["ACKs"])
            or s["RecoveryClass"] != "original_deadline_incomplete" or f["Match"] != s["RecoveryClass"]
            or f["SenderMode"] != 1 or f["ReceiverMode"] != 1 or f["SenderLatchedMode"] != 1
            or s["RTOs"] != 70 or s["Retransmits"] != 363 or s["SACKRecoveries"] != 72 or s["TLPRecoveries"]
            or s["PacketCount"] != 2059 or q["OfferedPackets"] != 1218 or q["AdmittedPackets"] != 844
            or q["DroppedPackets"] != 374 or q["FirstSecondDrops"] != 374 or q["Capacity"] != 125000
            or q["Rate"] != 1250000 or s["OneWayDelayNS"] != 50000000
            or f["StagedPackets"] or f["MaxProtectedWaitNS"] or f["EndNS"] != s["VirtualNS"]-first
            or s["AllPacketsSHA256"] != "98c0724b98db9f9590c94924f489f1d35e4dddd72895caea4aa06bf22211827b"
            or s["PayloadSHA256"] != "cc7be120a6b9ede5fdca259c7ecb82a1c8214f68eec1afaedee09d7299282b56"):
        raise ValueError("The original transport, drop policy, complete wire or original failure changed")
    events = f["Events"]
    peers = [e["Endpoint"] for e in events if "Endpoint" in e]
    if {e["Side"] for e in peers} != {0, 1} or not all(e["SACKEnabled"] and e["Timestamps"] for e in peers):
        raise ValueError("Missing negotiated timestamp/SACK or both endpoints")
    if max(e["At"] for e in events) > 2_100_000_000:
        raise ValueError("Observer exceeded its fixed 2.1s detail interval")
    losses = [e["Wire"] for e in events if e["Kind"] == "offer" and e["Wire"]["Dropped"]]
    discards = [e["Wire"] for e in events if e["Kind"] == "discard"]
    if (len(losses) != 374 or len(discards) != 374 or losses[0]["Seq"] != 335*1420
            or losses[0]["Captured"] != 523_572_800 or Counter(map(digest, losses)) != Counter(map(digest, discards))):
        raise ValueError("Not every original refusal was joined to its actual discard")
    timed = next(r for r in s["RecoveryTrace"] if r["RTOs"])
    i = next(i for i, e in enumerate(events) if e.get("Action", {}).get("Path") == "rto_expiry_pre_backoff")
    expiry = events[i]
    ex_at, action = expiry["At"], expiry["Action"]
    w = action["Watch"]
    if (timed["AtNS"] - first != 1_818_839_360 or not 0 <= timed["AtNS"]-first-ex_at <= 1_000_000
            or action["Trigger"] is not None or w["Focus"] != (base + 651*1420) % 2**32
            or w["RTO"] <= 0 or w["ResendEnabled"] or not w["Head"]):
        raise ValueError("First true RTO, historical fence or watched byte unverified")
    before = events[:i]
    timer_ops = [e for e in before if e.get("Action", {}).get("Path", "").startswith(("arm_", "disable_"))]
    if not timer_ops:
        raise ValueError("Missing the preceding resend-timer operation")
    arm = timer_ops[-1]
    if (not arm["Action"]["Path"].startswith("arm_") or not arm["Action"]["Watch"]["ResendEnabled"]
            or w["ResendTarget"] != arm["Action"]["Watch"]["ResendTarget"]
            or w["ResendTarget"]-first != arm["At"]+arm["Action"]["Watch"]["RTO"]
            or ex_at < w["ResendTarget"]-first):
        raise ValueError("Last timer arm, requested target or true expiry is inconsistent")
    input_before = [e["Wire"] for e in before if e["Kind"] == "input"]
    wire_edge = covered([(p["Seq"], p["End"]) for p in input_before if not p["Side"] and p["Payload"]])
    receiver = next(e for e in reversed(before) if e.get("Endpoint", {}).get("Side") == 1)
    sender = next(e for e in reversed(before) if e.get("Endpoint", {}).get("Side") == 0)
    if receiver["Endpoint"]["RcvNxt"] != wire_edge:
        raise ValueError(f"Cannot join receiver holdback to the physical first hole: {wire_edge}")
    first_ack = max((p["Ack"] for p in input_before if p["Side"] and p["Flags"] & 16), default=None)
    if first_ack != action["Una"]:
        raise ValueError("Latest cumulative ACK does not match the sender at true RTO")
    since = [e for e in events if e["Kind"] == "input" and e["Wire"]["Side"] == 1
             and e["Wire"]["Ack"] > action["Una"] and e["At"] >= ex_at]
    if not since:
        raise ValueError("No advancing ACK after first RTO within the fixed observer bound")
    advance = since[0]
    original_before = [e["Wire"] for e in before if e["Kind"] == "offer" and e["Wire"]["Side"] == 0
                       and e["Wire"]["Payload"] and not e["Wire"]["Retransmit"]]
    high = max(p["End"] for p in original_before)
    next_received = min((p["Seq"] for p in input_before if p["Side"] == 0 and p["Payload"]
                         and p["Seq"] > wire_edge), default=high)
    watched_original = [p for p in original_before if p["Seq"] <= 651*1420 < p["End"]]
    if len(watched_original) != 1:
        raise ValueError("Cannot identify the original transmission containing the watched byte")
    original = watched_original[0]
    ranges = {"sender_head": ((w["Head"]["Start"]-base) % 2**32, (w["Head"]["End"]-base) % 2**32),
              "wire_missing": (wire_edge, next_received) if wire_edge < next_received else None,
              "predeclared": (original["Seq"], original["End"])}
    readout = {}
    for start, end in sorted(span for span in set(ranges.values()) if span is not None):
        if not start < end <= high:
            raise ValueError(f"No nonempty transmitted interval: [{start},{end})")
        offers = [e["Wire"] for e in events if e["Kind"] == "offer" and e["Wire"]["Side"] == 0
                  and e["Wire"]["Payload"] and e["Wire"]["Seq"] < end and start < e["Wire"]["End"]]
        ingress = [e["Wire"] for e in events if e["Kind"] == "input" and e["Wire"]["Side"] == 0
                   and e["Wire"]["Payload"] and e["Wire"]["Seq"] < end and start < e["Wire"]["End"]]
        # Include resegmented retries even when only the end of their payload
        # overlaps. A sender success, a serializer drop and ingress are distinct.
        if (not offers or any(not p["Dropped"] and p["Arrival"] <= 2_100_000_000 and p not in ingress for p in offers)
                or any(p not in offers for p in ingress)):
            raise ValueError(f"Unmatched offer/admission/ingress for [{start},{end})")
        readout[f"{start}:{end}"] = {"first": start, "end": end, "offers": offers, "receiver_input": ingress,
                                    "positive_sender_actions": [e for e in events if "Action" in e
                                    and e["Action"].get("Range", {}).get("Start", 2**31) < end
                                    and start < e["Action"].get("Range", {}).get("End", 0)]}
    true_rack_scans = [e for e in before if e.get("Action", {}).get("Path") == "rack_scan"
                       and e["Action"].get("Watch") and e["Action"]["Watch"]["Segment"]]
    # Keep the last scan and evidence whenever the gate's predicates change;
    # the full authenticated native log retains all scans, peers and timers.
    scans = []
    for e in true_rack_scans:
        snap = e["Action"]["Watch"]
        seg = snap["Segment"]
        ordered = (seg["LastXmit"], (seg["End"]-base) % 2**32) < (snap["RackXmit"], (snap["RackEnd"]-base) % 2**32)
        remaining = seg["LastXmit"] - snap["RackEvalNS"] + snap["RackRTT"] + snap["ReoWindow"] + snap["ClockResolution"]
        eligible_now = (remaining < 0) or (remaining == 0 and snap["ClockResolution"] == 0)
        gates = [seg["Lost"], seg["ScoreboardSacked"], ordered, eligible_now,
                 e["Action"]["Outstanding"] >= e["Action"]["Cwnd"]]
        if not scans or gates != scans[-1]["gates"] or e is true_rack_scans[-1]:
            scans.append({"at": e["At"], "gates": gates, "remaining_ns": remaining, "event": e})
    return {"full_sample_sha256": digest(s), "events": len(events), "all_wire_sha256": s["AllPacketsSHA256"],
            "original_fifo": q, "outcome": {"virtual_ns": s["VirtualNS"], "delivered": s["Bytes"],
            "retransmits": s["Retransmits"], "rtos": s["RTOs"], "incomplete": s["Bytes"] != s["OfferedBytes"]},
            "first_true_rto": expiry, "stats_observed_at": timed["AtNS"]-first,
            "last_arm": arm, "timeout_after_target_ns": ex_at-(w["ResendTarget"]-first),
            "sequence_base": base, "first_data_ns": first, "ranges": ranges, "packet_histories": readout,
            "last_sender_probe": sender, "last_receiver_probe": receiver, "next_advancing_ack": advance,
            "first_rto_reset": next(e for e in events[i:] if e.get("Action", {}).get("Path") == "rto_reset"),
            "wire_edge_at_rto": wire_edge, "last_wire_high_ack": first_ack,
            "drop_original_vs_retry": dict(Counter("retry" if p["Retransmit"] else "original" for p in losses)),
            "watch_scan_gates_lost_sacked_ordered_timeeligible_cwndblocked": scans}


def measure(samples: dict) -> dict:
    names = [f"{ROOT}/{n}" for n in (ROLE, "one_repair")]
    if set(samples) != set(names):
        raise ValueError("Expected exactly the native control and single-repair treatment")
    control, treatment = (samples[n] for n in names)
    cf, tf = control["Flight"], treatment["Flight"]
    # Restore the exact authenticated, old observation layout to bind the
    # complete native control. Keep the new, read-only witness separately.
    historical = dict(control, Flight={k: v for k, v in cf.items() if k != "OneCredit"})
    if digest(historical) != "897ce4715dd6372700b9b717bf8ea17b33b272df2d5d1a76f34adf9a452d980b":
        raise ValueError("Complete native control changed relative to the retained original")
    reference = measure_original({names[0]: historical})
    rows = [cf["Events"], tf["Events"]]
    base = control["Startup"]["DataSequence"]
    first = control["Startup"]["FirstPayloadNS"]
    original_deadline = reference["first_true_rto"]["At"]
    cut = 6433
    prefix_hash = "0dbbbb00b4a5110c269c8bfa0547cf6c0333f547ba4612c57b1d91cf3f658105"
    def totals(row):
        q = row["ForwardQueue"]
        return {"delivered": row["Bytes"], "accepted": row["AcceptedBytes"], "offered": row["OfferedBytes"],
                "payload_sha256": row["PayloadSHA256"], "all_wire_sha256": row["AllPacketsSHA256"],
                "rtos": row["RTOs"], "retransmits": row["Retransmits"], "virtual_ns": row["VirtualNS"],
                "queue": q, "finished": row["Bytes"] == row["OfferedBytes"]}
    result = {"control": totals(control), "treatment": totals(treatment), "old_rto_ns": original_deadline,
              "historical_full_control_sha256": digest(historical), "cut": cut, "prefix_sha256": prefix_hash,
              "control_witness": cf.get("OneCredit"), "treatment_witness": tf.get("OneCredit")}
    for case in (control, treatment):
        q = case["ForwardQueue"]
        f = case["Flight"]
        if (case["Algorithm"] != "cubic" or case["Startup"]["MSS"] != 1420 or case["OfferedBytes"] != 16 << 20
                or not all(a["Plus"] for a in case["Startup"]["ACKs"])
                or q["Rate"] != 1_250_000 or q["Capacity"] != 125_000 or case["OneWayDelayNS"] != 50_000_000
                or f["SenderMode"] != 1 or f["ReceiverMode"] != 1 or f["SenderLatchedMode"] != 1
                or f["StagedPackets"] or f["MaxProtectedWaitNS"] or not all(
                    e["Endpoint"]["SACKEnabled"] and e["Endpoint"]["Timestamps"] for e in f["Events"] if "Endpoint" in e)
                or case["Bytes"] < case["OfferedBytes"] and not 30_000_000_000 <= case["VirtualNS"] <= 30_001_000_000):
            raise ValueError("The offered finite path, negotiated sender, or absolute deadline changed")
    if digest(rows[0][:cut]) != prefix_hash:
        raise ValueError("Control first-flight and full packet/endpoint prefix do not match historical events")
    split = next((i for i, (left, right) in enumerate(zip(*rows)) if left != right), min(map(len, rows)))
    result["first_event_difference"] = {"index": split, "control": rows[0][split:split+1], "treatment": rows[1][split:split+1]}
    witness = cf.get("OneCredit")
    if (not witness or witness != tf.get("OneCredit") or witness["Index"] != cut or witness["Requests"] != 1
            or split < cut or digest(rows[1][:cut]) != prefix_hash):
        return dict(result, adjudication="unmatched_prefix_or_witness")
    c = witness["Decision"]
    trigger = rows[0][cut-1]["Action"]
    if (rows[0][cut-1]["At"] != 1_540_661_760 or trigger["Path"] != "rack_recovery_scan" or
            trigger["Trigger"]["Ack"] != 651*1420 or c["AtNS"]-first != 1_540_661_760 or
            c["PreviousUna"]-base != 650*1420 or c["Una"]-base != 651*1420 or c["Nxt"]-base != 853*1420 or
            c["MSS"] != 1420 or c["Delivered"] != 1420 or c["SackBefore"] != 1420 or c["SackAfter"] != 1420 or
            c["Span"] != 202*1420 or c["Sacked"] != 1420 or c["MarkedOriginal"] != 201*1420 or
            c["PossibleBytes"] or c["Outstanding"] != 201 or c["Cwnd"] != 151 or c["Ssthresh"] != 148 or not c["Eligible"]):
        return dict(result, adjudication="historical_one_credit_preconditions_not_met")
    if any(e.get("Action", {}).get("Path") == "one_credit_granted" for e in rows[0]):
        raise ValueError("Control granted a diagnostic repair")
    # This split must be an actual sender grant between the shared native
    # RACK scan and the control's ACK exit. Independently retain wire effect.
    treatment_grants = [e for e in rows[1] if e.get("Action", {}).get("Path") == "one_credit_granted"]
    wanted = {"Start": 651*1420, "End": 652*1420}
    if (split != cut or rows[0][split].get("Action", {}).get("Path") != "ack_end" or
            len(treatment_grants) != 1 or rows[1][split] != treatment_grants[0] or
            treatment_grants[0]["Action"]["Trigger"] != trigger["Trigger"] or
            treatment_grants[0]["Action"]["Range"] != wanted):
        return dict(result, adjudication="missing_or_different_first_intervention")
    def effect(events):
        suffix = events[cut:]
        overlaps = lambda w: w["Side"] == 0 and w["Payload"] and w["Seq"] < wanted["End"] and wanted["Start"] < w["End"]
        offers = [e["Wire"] for e in suffix if e["Kind"] == "offer" and overlaps(e["Wire"])]
        inputs = [e["Wire"] for e in suffix if e["Kind"] == "input" and overlaps(e["Wire"])]
        discards = [e["Wire"] for e in suffix if e["Kind"] == "discard" and overlaps(e["Wire"])]
        if any(w not in offers for w in inputs+discards) or any(
                w["Arrival"] <= 2_100_000_000 and w not in (discards if w["Dropped"] else inputs) for w in offers):
            raise ValueError("Cannot join target offer to its actual acceptance/discard or peer ingress")
        advances = [e for e in suffix if e["Kind"] == "input" and e["Wire"]["Side"] == 1 and e["Wire"]["Ack"] >= wanted["End"]]
        peer_ack = [e for e in suffix if e.get("Endpoint", {}).get("Side") == 0 and e["Endpoint"]["Una"] >= wanted["End"]]
        rtos = [e for e in suffix if e.get("Action", {}).get("Path") == "rto_expiry_pre_backoff"]
        retries = [e for e in suffix if e.get("Action", {}).get("Range") == wanted
                   and e["Action"]["Path"] in ("retransmit", "retry_stack_accepted", "retry_stack_error")]
        return {"offers": offers, "arrivals": inputs, "discards": discards, "sender_attempts": retries,
                "first_advance": advances[:1], "sender_advance": peer_ack[:1], "first_true_rto_in_window": rtos[:1],
                "acked_before_old_deadline": bool(advances and peer_ack and advances[0]["At"] < original_deadline and peer_ack[0]["At"] < original_deadline)}
    before = rows[0][:cut]
    result["before_credit"] = {"forward_offers": sum(e["Kind"] == "offer" and e["Wire"]["Side"] == 0 and bool(e["Wire"]["Payload"]) for e in before),
                               "drops": sum(e["Kind"] == "discard" for e in before),
                               "received_prefix": covered([(e["Wire"]["Seq"], e["Wire"]["End"]) for e in before
                                                         if e["Kind"] == "input" and e["Wire"]["Side"] == 0 and e["Wire"]["Payload"]]),
                               "rtos": sum(e.get("Action", {}).get("Path") == "rto" for e in before)}
    result["target_range"] = wanted
    result["control_effect"], result["treatment_effect"] = map(effect, rows)
    result["adjudication"] = ("matched_advance_before_old_rto" if result["treatment_effect"]["acked_before_old_deadline"]
                              else "matched_no_certified_advance_before_old_rto")
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
        raise ValueError("Unexpected original FIFO qualification owners")
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
                raise RuntimeError(f"{phase}: native first-RTO owner did not pass")
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
