"""Run one real two-stack M003 transfer per cell on the same native host."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

OUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
ARMS = ("stock", "deadline", "select", "both")
SCHEDULES = ("timely", "held")
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
        dropped = sorted((rel(e["seq"]), e["length"]) for e in tx if e["dropped"])
        row["same_three_drops"] = dropped == [(r["Offset"], r["Length"]) for r in seen[:3]]
        packets = [e for e in tx if rel(e["seq"]) == head and e["length"] == seen[0]["Length"]]
        row["head_copies_ms"] = [round(1000 * (e["seconds"]-at), 3) for e in packets]
        row["extra_head_repairs"] = len(packets) - 2
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
        expected_ends = {r["Offset"] + r["Length"] for r in seen[6:]}
        fresh_entries = [s for s in signals if s["Kind"] == "rack-ack-new-sack" and
                         rel(s["SACKRight"]) in expected_ends and not s["Lost"]]
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
        row["valid"] = measured and row["same_three_drops"] and bool(data["probes"]) and at > 0 and bool(packets)
        row["mechanism"] = {"deadline": row["early_actual_timeout"], "queued_sack": row["head_after_entry"]}
        return row
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        row.update({"finished": time.time(), "error": type(e).__name__, "valid": False})
        return row


def main():
    mode, *files = sys.argv[1:]
    binaries = [Path(name) for name in files]
    rows = []
    if mode == "screen":
        for schedule in SCHEDULES:
            row = observe(binaries[0], schedule, "screen-" + schedule)
            rows.append(row)
        usable = len(rows) == 2 and all(r.get("valid") for r in rows)
        # A schedule may isolate either effect. Run the factorial only if both
        # mechanisms were actually witnessed somewhere in the two stock screens.
        both_seen = usable and all(any(r["mechanism"][m] for r in rows) for m in ("deadline", "queued_sack"))
        active = {r["schedule"]: r["ranges"] for r in rows if both_seen and any(r["mechanism"].values())}
        write(OUT / "screen.json", {"schedules": active, "usable": usable, "baseline_series": BASE, "rows": rows})
        assert usable, "stock measurement failed; no activation conclusion"
        print("stock activation", sorted(active), flush=True)
        return
    screen_result = json.loads(binaries.pop().read_text())
    screen = screen_result["schedules"]
    assert screen_result["baseline_series"] == BASE and screen_result["usable"]
    assert all(r["binary"] == digest(binaries[0]) for r in screen_result["rows"])
    assert [b.name for b in binaries] == list(ARMS) and len({digest(b) for b in binaries}) == 4
    assert screen and len(binaries) == 4
    for i, binary in enumerate(binaries):
        rows.append(observe(binary, "control", f"control-{ARMS[i]}"))
    # A four-period balanced Latin square: each arm once in every position.
    for rep, order in enumerate(((0,1,3,2), (1,2,0,3), (2,3,1,0), (3,0,2,1))):
        schedules = list(screen) if rep % 2 == 0 else list(reversed(screen))
        for schedule in schedules:
            for i in order:
                rows.append(observe(binaries[i], schedule, f"r{rep}-{schedule}-{ARMS[i]}", screen[schedule]))
                write(OUT / "comparison.json", {"rows": rows, "verified": False})
    verified = all(r.get("valid") and r.get("exit") == 0 and r.get("expected_bytes") == 524288
                   and (not r.get("complete") or r.get("integrity")) for r in rows)
    write(OUT / "comparison.json", {"schedules": list(screen), "baseline_series": BASE, "verified": verified, "rows": rows})
    print("attempted transfers", len(rows), "complete", sum(bool(r.get("complete") and r.get("integrity")) for r in rows), flush=True)
    assert verified


if __name__ == "__main__":
    main()
