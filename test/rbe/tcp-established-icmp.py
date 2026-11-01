"""Check both IP families, both consumers of SO_ERROR and live data transfer."""
import json
import os
from pathlib import Path
import re
import sys


def observe(path):
    if not (path / "exit").exists():
        return {"verified": False}
    text = (path / "bazel.log").read_text(errors="replace")
    events = [json.loads(line) for line in (path / "bep").read_text().splitlines()] if (path / "bep").exists() else []
    inv = [e["started"]["uuid"] for e in events if "started" in e]
    summaries = {e["id"]["testSummary"]["label"]: e["testSummary"]["overallStatus"] for e in events if "testSummary" in e}
    pattern = r"STALE_ICMP ack_barrier=true seq_delta=3 stale_poll=(\d+) stale_so=(\d+) live_poll=(\d+) live_so=(\d+) new_data=true"
    rows = sorted(set(re.findall(pattern, text)))
    exp = ("0", "0", "8", "113") if path.name == "native" else ("8", "113", "8", "113")
    rc = int((path / "exit").read_text())
    inv = []
    summaries = {}
    for line in (path / "bep").read_text().splitlines():
        event = json.loads(line)
        if "started" in event: inv.append(event["started"]["uuid"])
        if "testSummary" in event: summaries[event["id"]["testSummary"]["label"]] = event["testSummary"]["overallStatus"]
    ok = rows == [exp] and rc == 0 and list(summaries.values()) == ["PASSED"]
    return {"verified": ok, "exit": rc, "invocations": inv, "summaries": summaries, "rows": rows, "expected": exp, "fixture": (path / "fixture").read_text().split()[0], "start": (path / "start").read_text().strip(), "finish": (path / "finish").read_text().strip(), "diagnostics": [l[:300] for l in text.splitlines() if l.startswith(("ERROR:", "FAIL:", "--- FAIL:")) or "undefined:" in l][:6]}


if __name__ == "__main__":
    root = Path(sys.argv[1])
    arms = {name: observe(root / name) for name in ["native", "netstack"]}
    good = all(v["verified"] for v in arms.values()) and len({v.get("fixture") for v in arms.values()}) == 1
    (root / "verdict.json").write_text(json.dumps({"verified": good, "source": os.environ["CANDIDATE"], "arms": arms}, indent=2)+"\n")
    if len(sys.argv) == 2:
        raise SystemExit(not good)
