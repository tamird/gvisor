"""Compare only observed copies of the missing segment on the original flight."""

import hashlib
import json
from pathlib import Path
import sys
import zipfile


def observations(record, mode):
    assert record["status"] == "PASS" and not record["missing"] and not record["errors"]
    events = [
        (fields[0], dict(field.split("=", 1) for field in fields[1:]))
        for line in record["observations"]
        if (fields := line.split())[1] == f"mode={mode}"
    ]
    states = {row["phase"]: row for event, row in events if event == "FENCE_STATE"}
    mss = int(states["before_feedback"]["mss"])
    acks = [(int(row["cumulative"]), row["sack"]) for event, row in events if event == "FENCE_ACK"]
    assert acks == [(mss, "-1--1"), (mss, "-1--1" if mode == "repeat_ack" else f"{2*mss}-{3*mss}")]
    assert states["before_timeout"]["ca"] == "0" and all(states[phase]["ca"] == "4" for phase in ("first_timeout", "before_feedback"))
    wire = [row for event, row in events if event == "FENCE_WIRE"]
    for phase, offset in (("first_timeout", 0), ("discarded_repair", mss), ("later_repair", 2*mss)):
        assert any(p["phase"] == phase and int(p["bytes"]) == mss and int(p["offset"]) == offset for p in wire)
    copies = [
        p for p in wire if p["phase"] in ("after_ack", "later_retry") and int(p["offset"]) == mss and int(p["bytes"]) == mss
    ]
    assert copies and sum(event == "FENCE_DONE" for event, _ in events) == 1
    return {"copies": copies, "before": states["before_feedback"], "after": states["after_ack"], "retry": states.get("later_retry")}


def main():
    archive, root = map(Path, sys.argv[1:])
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest == "bd42d58c3c05a7e8237cd42137992dd014ac488cd5f9f56bcc4ca83817d4087d"
    with zipfile.ZipFile(archive) as z:
        rows = {name: json.loads(z.read(name + ".json")) for name in ("linux", "stock", "strict")}
    rows["two_guard"] = json.loads((root / "two_guard.json").read_text())
    assert rows["strict"]["source"] == "a35b6af25962f49d24c8d9bac6dad4c276fbf2dd"
    assert rows["two_guard"]["source"] == "0639f2d26e1504fae2d5239205f2a3909691d1a1"
    assert rows["linux"]["source"] == rows["stock"]["source"] == "21f8589cbf0a7ae132ee7961e1ff15e38bf92200"
    result = {name: {mode: observations(row, mode) for mode in ("repeat_ack", "sack_repair")} for name, row in rows.items()}
    (root / "comparison.json").write_text(json.dumps({"original": digest, "observed": result}, indent=2) + "\n")


if __name__ == "__main__":
    main()
