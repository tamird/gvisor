"""Reduce the pinned single-loss experiment from native Bazel evidence."""
import hashlib
import json
from pathlib import Path

from image_workload import OWNER, observations


ROOT = "TestVirtualTCPSingleLossFrontier"
TARGET = 131072
BODY = bytes((i * 17 + i // 251) % 256 for i in range(4 << 20))


def analyze(sample: dict, cc: str, loss: bool) -> tuple[dict, int | None]:
    s = sample
    completed = s["EOF"] and s["FinACK"] and s["Pending"] == 0
    elapsed = s["End"] - s["FirstData"]
    if ((s["Algorithm"], s["KernelOS"], s["KernelArch"], s["Go"]) != (cc, "linux", "amd64", "go1.27.3")
            or s["Schedule"]["Name"] != ("frontier_loss" if loss else "frontier_control")
            or s["GuardACK"] or s["GuardTimer"] or s["Offered"] != len(BODY) or s["Accepted"] != len(BODY)
            or not 0 <= s["Delivered"] <= len(BODY) or not 0 <= s["Acknowledged"] <= len(BODY)
            or s["BodyHash"] != hashlib.sha256(BODY[:s["Delivered"]]).hexdigest()
            or completed and (s["Acknowledged"] != len(BODY) or s["Delivered"] != len(BODY))
            or not 0 < elapsed <= 35_000_000_000 or not completed and elapsed != 35_000_000_000):
        raise ValueError(f"{s['Case']}: unexpected sender or transfer result")
    events = s["Events"]
    wire = [(i, e["Wire"]) for i, e in enumerate(events) if "Wire" in e]
    ingress = [(i, e["Ingress"]) for i, e in enumerate(events) if "Ingress" in e]
    states = [(i, e["Sender"]) for i, e in enumerate(events) if "Sender" in e]
    syn = [(w["From"], w["Flags"], w["Permitted"]) for _, w in wire if w["Flags"] & 2]
    if (syn != [(0, 2, True), (1, 18, True)] or any(not w["TS"] for _, w in wire)
            or any(not r["Timestamps"] or not r["SACKEnabled"] for _, r in states)
            or s["WirePackets"] < len(wire) or len(s["WireHash"]) != 64
            or len(s["EchoNS"]) != 7 or any(not 90_000_000 <= ns <= 130_000_000 for ns in s["EchoNS"])
            or any(not 0 <= q <= 100 for q in s["MaxOccupancy"])
            or any(p["At"] != p["Packet"]["Due"] or p["Packet"]["Dropped"] for _, p in ingress)):
        raise ValueError(f"{s['Case']}: missing negotiated options or incorrect queue/ingress record")
    # The capture is continuous until the sender ACKs 320 KiB. Later it retains
    # only drops, along with SHA256/census for all physical offers. Every
    # admitted packet in the prefix has an independently reproducible due time.
    service = [0, 0]
    for _, w in wire:
        if w.get("QueueDrop"):
            continue
        side = w["From"]
        rate = (1_250_000, 12_500_000)[side]
        service[side] = max(service[side], w["At"]) + (w["IPBytes"] * 1_000_000_000 + rate - 1) // rate
        if not 40 <= w["IPBytes"] <= 1500 or service[side] + 50_000_000 != w["Due"]:
            raise ValueError(f"{s['Case']}: serialized service/propagation differs at {w}")
    queues = [(i, w) for i, w in wire if w.get("QueueDrop")]
    injected = [(i, w) for i, w in wire if w.get("Injected")]
    if (s["QueueDrops"] != [sum(w["From"] == side for _, w in queues) for side in (0, 1)]
            or s["Drops"] != len(queues) + len(injected) or any(w.get("Occupancy") != 100 for _, w in queues)
            or any(not w["Dropped"] for _, w in queues + injected) or len(injected) > int(loss)):
        raise ValueError(f"{s['Case']}: inconsistent physical drops")
    forward = [(i, w) for i, w in wire if w["From"] == 0 and w["Offer"]]
    seq = forward[0][1]["Seq"]
    offset = lambda x: (x - seq) % 2**32
    target_copies = [(i, w) for i, w in forward if offset(w["Seq"]) <= TARGET < offset(w["Seq"]) + w["Bytes"]]
    result = dict(completed=completed, elapsed_ns=elapsed, delivered=s["Delivered"], acknowledged=s["Acknowledged"],
                  eof=s["EOF"], fin_ack=s["FinACK"], pending=s["Pending"], payload_sha256=s["BodyHash"],
                  retransmissions=s["Retransmissions"], rtos=s["RTOs"], queue_drops=s["QueueDrops"],
                  queue_peak=s["MaxOccupancy"], wire_packets=s["WirePackets"], wire_sha256=s["WireHash"],
                  first_data=s["FirstData"], echo_ns=s["EchoNS"], seq=seq, target_copies=target_copies)
    reasons = []
    if not target_copies:
        reasons.append("target not observed")
    origin = target_copies[0] if target_copies else None
    if origin and (origin[1]["Retry"] or origin[1].get("QueueDrop") or bool(origin[1].get("Injected")) != loss):
        reasons.append("first target segment is not the designated original")
    if not loss:
        result.update(status="RECORDED", limitations=reasons)
        return result, origin and origin[0]
    kept = [(i, w) for i, w in target_copies if not w["Dropped"]]
    if not origin or injected != [origin] or len(kept) != 1 or len(target_copies) != 2:
        reasons.append("need exactly one discarded original and one admitted covering retry")
    if origin and kept:
        i0, first = origin
        ir, retry = kept[0]
        lower, upper = offset(first["Seq"]), offset(first["Seq"]) + first["Bytes"]
        if not retry["Retry"] or (retry["Seq"], retry["Bytes"]) != (first["Seq"], first["Bytes"]):
            reasons.append("repair does not reproduce the original byte interval")
        arrived = [(i, p) for i, p in ingress if p["Packet"] == retry]
        covers = [(i, w) for i, w in wire if i > ir and w["From"] == 1 and not w["Dropped"]
                  and w["Flags"] & 16 and offset(w["ACK"]) >= upper]
        cover = covers[0] if covers else None
        accepted = ([(i, p) for i, p in ingress if p["Packet"] == cover[1]] if cover else [])
        processing = ([(i, r) for i, r in states if accepted and i > accepted[0][0] and r["Kind"] == "ack_begin"
                       and r["At"] == accepted[0][1]["At"] and r["ACK"] == cover[1]["ACK"]
                       and offset(r["UNA"]) < upper] if cover else [])
        applied = ([(i, r) for i, r in states if accepted and i > accepted[0][0] and r["Kind"] == "ack_end"
                    and r["At"] == accepted[0][1]["At"] and r["ACK"] == cover[1]["ACK"]
                    and upper <= offset(r["UNA"]) <= len(BODY) + 1] if cover else [])
        if (len(arrived) != 1 or not cover or len(accepted) != 1 or len(processing) != 1 or len(applied) != 1
                or processing[0][0] >= applied[0][0] or arrived[0][0] >= cover[0]
                or any(p["Packet"] == first for _, p in ingress)):
            reasons.append("repair ingress and peer/sender covering ACK not all bound")
        sacks = [(i, p) for i, p in ingress if i0 < i < ir and p["Packet"]["From"] == 1
                 and offset(p["Packet"]["ACK"]) == lower and any(
                     upper <= offset(b["Start"]) < offset(b["End"]) for b in p["Packet"]["SACK"] or [])]
        callbacks = [(i, r) for i, r in states if i0 < i < ir and r["Kind"] in ("dupack_cc", "ack_cc", "timer_cc")]
        if not sacks or len(callbacks) != 1 or callbacks[0][0] < sacks[0][0]:
            reasons.append("no unique loss callback after an actual above-hole SACK and before the repair")
        first_terminal = applied[0][0] if applied else len(events)
        if any(i < first_terminal for i, _ in queues) or any(
                (i < i0 and r["Kind"].endswith("_cc")) or
                (i < first_terminal and r["Kind"] in ("rto_interrupt", "tlp_cc")) for i, r in states):
            reasons.append("queue drop, timeout, or TLP congestion callback contaminates the first episode")
        result.update(repair_ingress=arrived, above_hole_sacks=sacks, peer_cover=cover,
                      ack_ingress=accepted, sender_begin=processing, sender_applied=applied, loss_callbacks=callbacks)
        if len(callbacks) == 1:
            idx, before = callbacks[0]
            if before["Kind"] in ("ack_cc", "timer_cc"):
                last = [(i, r) for i, r in states if i < idx]
                scan_kind = {"ack_cc": "ack_scan", "timer_cc": "timer_scan"}[before["Kind"]]
                scan = last[-1][1] if last else {}
                mark = last[-2][1].get("Copy") if len(last) >= 2 else None
                if (scan.get("Kind") != scan_kind or scan.get("Lost") != 1 or scan["At"] != before["At"]
                        or not mark or mark["Count"] != 1 or mark["AlreadyLost"] or mark["Start"] != first["Seq"]
                        or offset(mark["End"]) != upper or not 0 <= first["At"] - mark["Sent"] <= 1_000_000):
                    reasons.append("RACK's callback is not bound to the single fresh original target copy")
                result.update(prior_scan=scan, prior_mark=mark)
            elif before["ACK"] != first["Seq"] or not before["SACK"]:
                reasons.append("duplicate-ACK callback does not identify the target hole")
            next_state = next(((i, r) for i, r in states if i > idx), None)
            if not next_state or next_state[1]["Kind"] != "enter_recovery" or next_state[1]["At"] != before["At"]:
                reasons.append("missing immediate post-controller threshold")
            else:
                after = next_state[1]
                mss = first["Bytes"]
                if (before["Cwnd"] < 16 or before["PeerWindow"] <= before["Cwnd"] * mss
                        or offset(before["NXT"]) >= len(BODY)):
                    reasons.append("small window, receiver bound, or demand exhausted")
                expected = max(2, int(0.7 * before["Cwnd"]) if cc == "cubic" else before["Flight"] // 2)
                result.update(post=next_state, predicted_threshold=expected,
                              observed_threshold=after["Threshold"], threshold_matches=expected == after["Threshold"])
    result.update(status="INCONCLUSIVE" if reasons else "OBSERVED", limitations=reasons)
    return result, origin and origin[0]


def measure(samples: dict) -> dict:
    result = {}
    expected = {f"{ROOT}/{cc}_frontier_{mode}" for cc in ("cubic", "reno") for mode in ("control", "loss")}
    if set(samples) != expected:
        raise ValueError(f"Expected four physical transfers, got {sorted(samples)}")
    for cc in ("cubic", "reno"):
        arms = {}
        for mode in ("control", "loss"):
            sample = samples[f"{ROOT}/{cc}_frontier_{mode}"]
            summary, index = analyze(sample, cc, mode == "loss")
            arms[mode] = (sample["Events"], index)
            result[f"{cc}_{mode}"] = summary
        (ctl, i), (loss, j) = arms["control"], arms["loss"]
        if i is not None and j is not None:
            match = i == j and ctl[:i] == loss[:j] and dict(ctl[i]["Wire"], Dropped=True, Injected=True) == loss[j]["Wire"]
        else:
            match = False
        result[f"{cc}_loss"]["matched_control_prefix"] = match
        if not match:
            result[f"{cc}_loss"]["limitations"].append("mirror did not share the complete state/wire prefix")
            result[f"{cc}_loss"]["status"] = "INCONCLUSIVE"
    return result


def run_frontier(invoke, evidence: Path, config: dict) -> None:
    if config["targets"] != [OWNER] or config["expected_sources"] != [config["source"]]:
        raise ValueError("Unexpected source or test owner")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"], "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]
    changed = ["pkg/tcpip/tests/integration/reno_burst_test.go", "pkg/tcpip/transport/tcp/connect.go",
               "pkg/tcpip/transport/tcp/protocol.go"]
    fmt = invoke("goimports", ["run", *options, "--run_in_cwd", "@org_golang_x_tools//cmd/goimports", "--",
                               "-format-only", "-d", *changed], "command", targets=[])
    if fmt["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").stat().st_size:
        raise RuntimeError("Canonical goimports check failed")
    selected = ["--strategy=TestRunner=remote", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=errors", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1", "--runs_per_test=2", f"--test_arg=-test.run=^{ROOT}$"]
    phase = "single-loss"
    verdict = invoke(phase, ["test", *options, *selected, OWNER], "test", targets=[OWNER])
    if verdict["status"] != "PASS" or verdict["source"] != config["source"]:
        raise RuntimeError("Native qualification did not pass on the selected source")
    result = observations(evidence, phase, 2, case_root=ROOT, reduce_samples=measure)
    result["source"] = config["source"]
    result["prediction_sha256"] = config["prediction_sha256"]
    (evidence / "single-loss-frontier.json").write_text(json.dumps(result, indent=2) + "\n")
