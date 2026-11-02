"""Follow one real peer SACK through the stock sender's loss decision."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

OUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
BASE = "b0d2148f41d2d35577b5fe759192f71947cc87fd"


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observe(binary, schedule, label, ranges=None):
    start = time.time()
    output = OUT / f"{label}.json"
    row = {"arm": binary.name, "schedule": schedule, "trial": label, "started": start, "binary": digest(binary)}
    command = [binary.absolute(), "-scenario", schedule, "-cc", "cubic", "-timestamps=true",
               "-deadline", "4s", "-output", output]
    ref = OUT / f"{label}.ranges"
    if ranges is not None:
        write(ref, ranges)
        command.extend(["-ranges", ref])
    try:
        proc = subprocess.run(command, stdout=subprocess.DEVNULL, timeout=9)
        row["exit"] = proc.returncode
        row["finished"] = time.time()
        data = json.loads(output.read_text())
        obs = json.loads(Path(str(output) + ".events.json").read_text())
        row.update({"received_bytes": data["received_bytes"], "expected_bytes": data["expected_bytes"],
                    "complete": data["complete"], "integrity": data["integrity_ok"], "seconds": data["duration_seconds"],
                    "delivered_Mbps": 8 * data["received_bytes"] / 1e6 / data["duration_seconds"],
                    "timeouts": data["counters"]["timeouts"], "fast_retransmit": data["counters"]["fast_retransmit"]})
        completed = row["complete"] and row["integrity"] and row["received_bytes"] == row["expected_bytes"] == 524288
        row["completion_goodput_Mbps"] = row["delivered_Mbps"] if completed else None
        measured = (row["exit"] == 0 and row["expected_bytes"] == 524288 and row["seconds"] > 0
                    and 0 <= row["received_bytes"] <= 524288 and (completed or data["read_error"])
                    and data["congestion_control"] == "cubic" and data["timestamps_offered"] and data["scenario"] == schedule)
        tx = [e for e in data["events"] if e["direction"] == "service_to_client" and e["length"]]
        row["data_copies_after_first"] = sum(e["copy"] > 1 for e in tx)
        row["injected_drops"] = data["injected_drops"]
        if schedule == "control":
            row["valid"] = measured and completed and row["injected_drops"] == 0
            return row
        seen = obs["Seen"] or []
        row["ranges"] = seen
        row["ranges_match"] = len(seen) == 8 and (ranges is None or seen == ranges) and all(
            a["Offset"] + a["Length"] == b["Offset"] for a, b in zip(seen, seen[1:]))
        if not row["ranges_match"]:
            row["valid"] = False
            return row
        origin = obs["Origin"]
        def rel(sequence):
            return (sequence - origin) % (1 << 32)
        head = seen[0]["Offset"]
        at = obs["Anchor"]
        dropped = sorted((rel(e["seq"]), e["length"], e["copy"]) for e in tx if e["dropped"])
        expected_drops = sorted([(r["Offset"], r["Length"], 1) for r in seen[:3]] +
                                [(head, seen[0]["Length"], copy) for copy in (2, 3)])
        row["dropped"] = dropped
        row["expected_drops"] = dropped == expected_drops and row["injected_drops"] == 5
        packets = [e for e in tx if rel(e["seq"]) == head and e["length"] == seen[0]["Length"]]
        row["head_copies_ms"] = [round(1000 * (e["seconds"]-at), 3) for e in packets]
        row["extra_head_repairs"] = len(packets) - 2 if len(packets) >= 2 else None
        advances = [e for e in data["events"] if e["direction"] == "client_to_service" and e["seconds"] >= at
                    and head + seen[0]["Length"] <= rel(e["ack"]) <= 524289]
        row["first_peer_ack_past_head_ms"] = round(1000 * (advances[0]["seconds"] - at), 3) if advances else None
        signals = [s for s in obs["Signals"] or [] if rel(s["Head"]) == head]
        row["signals"] = [{**s, "ms": round(1000*(s["At"]-at), 3)} for s in signals]
        repairs = [s for s in signals if s["Kind"] == "head-repair"]
        timeouts = [s for s in signals if s["Kind"] == "rto"]
        new_sacks = [e for e in data["events"] if e["direction"] == "client_to_service" and rel(e["ack"]) == head and e.get("sack")]
        row["sack_sequence"] = [{"ms": round(1000*(e["seconds"]-at), 3), "ranges":
                                [[rel(a), rel(b)] for a,b in e["sack"]]} for e in new_sacks]
        row["first_repair"] = bool(repairs)
        fresh_entries = [s for s in signals if s["Kind"] == "rack-ack-new-sack" and
                         s.get("Blocks") and not s["Lost"]]
        # Bind the same fresh SACK decision and its actual subsequent head copy to
        # one RTO cycle. A later timeout cannot provide either side of this witness.
        episodes = []
        for i, timeout in enumerate(timeouts):
            end = timeout["At"] + timeout["RTO"]
            if i + 1 < len(timeouts):
                end = min(end, timeouts[i+1]["At"])
            for e in fresh_entries:
                if timeout["At"] < e["At"] < end:
                    copies = [p["copy"] for p in packets if e["At"] <= p["seconds"] < min(e["At"]+.012, end)]
                    episodes.append({"rto_ms": (timeout["At"]-at)*1000, "entry_ms": (e["At"]-at)*1000,
                                     "received_sack_right": rel(e["SACKRight"]), "head_copies": copies})
        row["post_timeout_episodes"] = episodes
        row["head_after_entry"] = any(e["head_copies"] for e in episodes)
        row["early_actual_timeout"] = bool(repairs and timeouts and 0 < timeouts[0]["At"] - repairs[0]["At"] < repairs[0]["RTO"] - .015)
        seen_right = {end for sack in row["sack_sequence"] for _, end in sack["ranges"]}
        expected = [seen[6]] if schedule == "timely" else [seen[6], seen[7]]
        row["genuine_extensions"] = all(r["Offset"] + r["Length"] in seen_right for r in expected)
        row["valid"] = measured and row["expected_drops"] and bool(data["probes"]) and at > 0 and bool(packets)
        row["mechanism"] = {"deadline": row["early_actual_timeout"], "queued_sack": row["head_after_entry"]}
        return row
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        row.update({"finished": time.time(), "error": type(e).__name__, "valid": False})
        return row

def discriminator(data, obs):
    origin, anchor = obs["Origin"], obs["Anchor"]
    head, hole, donor = (117504, 118704), (127104, 128304), (140672, 141872)
    def rel(x):
        return (x-origin) % (1 << 32)
    def span(e):
        start = rel(e["seq"])
        return start, start+e["length"]
    def blocks(e):
        return [(rel(a), rel(b)) for a,b in (e or [])]
    def first(stream):
        return next(iter(stream), None)
    signals = [s for s in obs["Signals"] or [] if rel(s["Head"]) == head[0]]
    fwd = [p for p in data["events"] if p["direction"] == "service_to_client" and p["length"]]
    rev = [p for p in data["events"] if p["direction"] == "client_to_service"]
    a = {p["copy"]: p for p in fwd if span(p) == head}
    repaired = first(s for s in signals if s["Kind"] == "head-repair")
    rto = first(s for s in signals if s["Kind"] == "rto")
    donor_ack = first(e for e in rev if any(l <= donor[0] and donor[1] <= r for l,r in blocks(e.get("sack"))))
    entry = None
    if donor_ack:
        entry = first(s for s in signals if s["Kind"] == "rack-ack-new-sack"
                      and 0 < s["At"]-donor_ack["seconds"] < .030 and blocks(s["Blocks"]) == blocks(donor_ack.get("sack")))
    mark = None
    if entry:
        mark = first(s for s in signals if s["Kind"] == "rack-loss" and s["Stamp"] == entry["Stamp"]
                     and s["At"] <= entry["At"] and (rel(s["Hole"]["Start"]), rel(s["Hole"]["End"])) == hole)
    def one_original(range_, until):
        sent = [p for p in fwd if span(p) == range_ and p["seconds"] < until]
        return len(sent) == 1 and sent[0]["copy"] == 1 and not sent[0]["dropped"]
    clean = False
    if mark:
        m = mark["Hole"]
        clean = (m["Count"] == 1 and not m["AlreadyLost"] and rel(m["LatestEnd"]) == donor[1]
                 and m["HeadAge"] < m["LatestAge"] < m["HoleAge"] and m["HoleAge"] > m["RTT"]+m["Reo"]+m["Precision"])
    gates = {
        "first_head_repair_observed": bool(repaired and 2 in a),
        "oldest_repair_then_early_rto": bool(rto and repaired and 2 in a and 3 in a and repaired["At"] < rto["At"] < a[3]["seconds"]
                                            and a[2]["seconds"] < rto["At"] and rto["At"]-repaired["At"] < repaired["RTO"]-.015),
        "real_peer_sacked_only_after_rto": bool(rto and 3 in a and donor_ack and a[3]["seconds"] < donor_ack["seconds"]
                                                and rel(donor_ack["ack"]) == head[0] and .280 <= donor_ack["seconds"]-anchor < .365
                                                and one_original(donor, donor_ack["seconds"])),
        "clean_head_at_rack_entry_for_that_sack": bool(entry and not entry["Lost"]),
        "same_detector_marked_older_hole": bool(mark and clean),
        "head_sent_once_more_by_this_entry": False,
    }
    if entry and 4 in a:
        when = a[4]["seconds"]
        later = [s for s in signals if entry["At"] <= s["At"] < entry["At"]+.012 and s["Kind"] == "head-repair"]
        gates["head_sent_once_more_by_this_entry"] = bool(later and entry["At"] <= when < entry["At"]+.012
            and not any(s["Kind"] == "rto" and rto and rto["At"] < s["At"] <= when for s in signals)
            and not any(donor_ack and donor_ack["seconds"] < p["seconds"] <= when for p in rev)
            and not any(rto and rto["At"] < p["seconds"] <= when and head[1] <= rel(p["ack"]) <= 524289 for p in rev))
    # Follow the same detector call until the next observed peer packet or
    # loss/timer event. A missing head copy alone does not prove a repair.
    window = {"start_ms": None, "end_ms": None, "end_cause": None, "marked": [], "repaired": [],
              "head_copies": [], "head_successful_sends": [], "uninterrupted": False}
    if donor_ack and entry:
        causes = [(p["seconds"], "next peer packet") for p in rev if p["seconds"] > donor_ack["seconds"]]
        causes.extend((s["At"], s["Kind"]) for s in signals if s["At"] > entry["At"]
                      and (s["Kind"] in ("rto", "rack-timer", "rack-ack", "rack-ack-new-sack") or
                           s["Kind"] == "rack-loss" and s["Stamp"] != entry["Stamp"]))
        if causes:
            end, cause = min(causes)
            begin = entry["At"]
            window.update(start_ms=1000*(begin-anchor), end_ms=1000*(end-anchor), end_cause=cause,
                          uninterrupted=bool(3 in a and a[3]["seconds"] < donor_ack["seconds"] < begin < end
                              and not any(a[3]["seconds"] < p["seconds"] < begin for p in fwd if span(p)==head)
                              and not any(rto and rto["At"] < s["At"] < begin and s["Kind"]=="rto" for s in signals)))
            marks = []
            for sig in signals:
                if sig["Kind"] != "rack-loss" or sig["Stamp"] != entry["Stamp"] or sig["At"] > begin:
                    continue
                h = sig["Hole"]
                region = (rel(h["Start"]), rel(h["End"]))
                if (region[0] >= head[1] and region[0] < region[1] and h["Count"] == 1 and not h["AlreadyLost"]
                        and rel(h["LatestEnd"]) == donor[1] and h["HeadAge"] < h["LatestAge"] < h["HoleAge"]
                        and h["HoleAge"] > h["RTT"] + h["Reo"] + h["Precision"] and one_original(region, begin)):
                    marks.append(region)
            window["marked"] = marks
            window["repaired"] = [{"range": span(p), "copy": p["copy"], "ms": 1000*(p["seconds"]-anchor)}
                                  for p in fwd if span(p) in marks and begin <= p["seconds"] < end and p["copy"] == 2 and not p["dropped"]]
            window["head_copies"] = [p["copy"] for p in fwd if span(p)==head and begin <= p["seconds"] < end]
            window["head_successful_sends"] = [1000*(s["At"]-anchor) for s in signals if s["Kind"]=="head-repair" and begin <= s["At"] < end]
            # The next peer transmit precedes its arrival at the sender. Inspect the
            # head beyond it, through a subsequent sender loss decision (or the last
            # packet of the complete transfer when the sender made none).
            decisions = [(s["At"], s["Kind"]) for s in signals if s["At"] > begin and
                         (s["Kind"] in ("rto", "rack-timer", "rack-ack", "rack-ack-new-sack") or
                          s["Kind"] == "rack-loss" and s["Stamp"] != entry["Stamp"])]
            stop, why = min(decisions) if decisions else (max(p["seconds"] for p in data["events"]), "last recorded packet")
            window["sender_boundary_ms"], window["sender_boundary"] = 1000*(stop-anchor), why
            window["later_head_before_sender_boundary"] = [p["copy"] for p in fwd if span(p)==head and begin <= p["seconds"] < stop]
            window["later_head_sends_before_sender_boundary"] = [1000*(s["At"]-anchor) for s in signals
                                                                if s["Kind"]=="head-repair" and begin <= s["At"] < stop]
    progress = first(p for p in rev if p["seconds"] >= anchor and head[1] <= rel(p["ack"]) <= 524289)
    window["first_peer_ack_covering_head_ms"] = None if progress is None else 1000*(progress["seconds"]-anchor)
    window["head_transmits_through_first_peer_progress"] = [{"copy": p["copy"], "ms": 1000*(p["seconds"]-anchor)}
        for p in fwd if span(p)==head and progress is not None and p["seconds"] <= progress["seconds"]]
    stamp = lambda e,key: None if e is None else round(1000*(e[key]-anchor),3)
    context = all(passed for name, passed in gates.items() if name != "head_sent_once_more_by_this_entry")
    outcome = ("observed_extra_head" if all(gates.values()) else
               "no_qualifying_extra_head_observed" if context else "intended_context_not_established")
    return {"window": window, "outcome": outcome, "activation": all(gates.values()), "gates": gates,
            "missing": [name for name,passed in gates.items() if not passed],
            "first_head_repair_ms": stamp(repaired,"At"), "timeout_ms": stamp(rto,"At"),
            "donor_ack_ms": stamp(donor_ack,"seconds"), "donor_ack_blocks": blocks(donor_ack.get("sack")) if donor_ack else [],
            "entry_ms": stamp(entry,"At"), "marked_ms": stamp(mark,"At"), "marked": mark["Hole"] if mark else None,
            "head_copies_ms": [stamp(a[k],"seconds") for k in sorted(a)]}


def main():
    *binaries, frozen = map(Path, sys.argv[1:])
    assert [p.name for p in binaries] == ["stock", "select"]
    ranges = json.loads(frozen.read_text())
    assert len(ranges) == 8 and ranges[0] == {"Offset": 117504, "Length": 1200}
    rows = []
    for binary in binaries:
        label = f"held-{binary.name}"
        row = observe(binary, "held", label, ranges)
        rows.append(row)
        try:
            path = OUT / (label+".json")
            data = json.loads(path.read_text())
            obs = json.loads(Path(str(path)+".events.json").read_text())
            row["observation"] = discriminator(data, obs)
            row["received_sha256"] = obs["PayloadSHA256"]
            offset = obs["Anchor"]
            row["head_send_paths"] = [{"copy": e["Count"], "ms": 1000*(e["Time"]-offset), "callers": e["Callers"]}
                                      for e in obs["HeadEvents"] if e["Kind"] == "successful-head-send"]
            start = ranges[0]["Offset"]
            row["head_packets"] = [{"copy": e["copy"], "dropped": e["dropped"], "ms": 1000*(e["seconds"]-offset)}
                                   for e in data["events"] if e["direction"] == "service_to_client" and
                                   (e["seq"]-obs["Origin"]) % (1<<32) == start and e["length"] == ranges[0]["Length"]]
            sent = [p["copy"] for p in row["head_packets"] if p["copy"] >= 2]
            paths = row["head_send_paths"]
            row["complete_send_attribution"] = bool(sent and sent == [h["copy"] for h in paths] and
                                                    all(h["callers"] for h in paths))
            row["verified_prefix"] = row["received_sha256"] == hashlib.sha256(bytes(
                (i*17+3) % 251 for i in range(row["received_bytes"]))).hexdigest()
        except (OSError, ValueError, KeyError, TypeError) as error:
            row["observation_error"] = f"{type(error).__name__}: {error}"
    # A real stalled transfer or a missed activation is a reportable observation.
    verified = all(r.get("valid") and r.get("verified_prefix") and "observation" in r and
                   r.get("head_send_paths") and r.get("head_packets") for r in rows)
    contexts = []
    holes = []
    for r in rows:
        obs = r.get("observation", {})
        g, w = obs.get("gates", {}), obs.get("window", {})
        contexts.append(all(g.get(k) for k in ("first_head_repair_observed", "oldest_repair_then_early_rto",
                            "real_peer_sacked_only_after_rto", "clean_head_at_rack_entry_for_that_sack",
                            "same_detector_marked_older_hole")) and w.get("uninterrupted"))
        holes.append({tuple(e["range"]) for e in w.get("repaired", [])})
    common = sorted(holes[0] & holes[1])
    matched = bool(verified and all(contexts) and common and rows[0]["observation"]["activation"] and
                   not rows[1]["observation"]["window"]["later_head_before_sender_boundary"] and
                   not rows[1]["observation"]["window"]["later_head_sends_before_sender_boundary"])
    verdict = {"verified": bool(verified), "activation_matched": matched, "common_older_holes_repaired": common,
               "rows": rows, "baseline_series": BASE, "transfers": 2}
    write(OUT / "comparison.json", verdict)
    assert verified, [(r["arm"], r.get("error"), r.get("observation_error"), r.get("valid")) for r in rows]


if __name__ == "__main__":
    main()
