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
    msg = {}
    for row in re.findall(r"ESTAB_MSG family=(\d) opt=([01]) first=(true|false) poll=(\d+) so=(\d+) afterfirst=(\d+) n=(-?\d+) errno=(\d+) flags=(\d+) cbytes=(\d+) level=\d+ type=\d+ ee=\d+ origin=\d+ itype=\d+ code=\d+ again=(\d+) final=(\d+)", text):
        msg["-".join(row[:3])] = list(map(int, row[3:]))
    io = {"-".join(r[:2]): list(map(int, r[2:])) for r in re.findall(r"ICMP_IO family=(\d) op=(read|write) before=(\d+) n=(-?\d+) errno=(\d+) after=(\d+) so=(\d+)", text)}
    expected = {}
    expected_io = {}
    for family, err in [(4, 113), (6, 101)]:
        for opt in [0, 1]:
            for msgfirst in ["true", "false"]:
                expected[f"{family}-{opt}-{msgfirst}"] = [8*opt, err, 8*opt*(msgfirst=="true"), -1, 11, 0, 0, 11, 0]
        for op in ["read", "write"]:
            expected_io[f"{family}-{op}"] = [8, -1, err, 0, 0]
    mlive = sorted({"-".join(m) for m in re.findall(r"ESTAB_MSG_LIVE family=(\d) opt=([01]) first=(true|false)", text)})
    ilive = sorted({"-".join(m) for m in re.findall(r"ICMP_IO_LIVE family=(\d) op=(read|write)", text)})
    rc = int((path / "exit").read_text())
    terminal = "--- PASS: TestTCPConnectICMPError" in text
    stale = sorted(set(re.findall(r"STALE_ICMP ack_barrier=true seq_delta=3 stale_poll=(\d+) stale_so=(\d+) live_poll=(\d+) live_so=(\d+) new_data=true", text)))
    nogo = any(e.get("id", {}).get("targetCompleted", {}).get("label") == "//pkg/tcpip/transport/tcp:tcp_nogo" and e.get("completed", {}).get("success") for e in events)
    ok = stale == [("0", "0", "8", "113")] and nogo and not rc and msg == expected and io == expected_io and mlive == sorted(expected) and ilive == sorted(expected_io) and terminal and len(inv) == 1 and summaries == {"//test/packetimpact/tests:tcp_connect_icmp_error_netstack_test": "PASSED", "//pkg/tcpip/transport/tcp:tcp_nogo": "PASSED"}
    return {"verified": ok, "stale": stale, "nogo": nogo, "exit": rc, "invocations": inv, "summaries": summaries, "pending_socket_error": msg, "io_error": io, "continued_msg": mlive, "continued_io": ilive, "terminal": terminal, "fixture": (path / "fixture").read_text().split()[0], "start": (path / "start").read_text().strip(), "finish": (path / "finish").read_text().strip(), "diagnostics": [l[:380] for l in text.splitlines() if ("pending error not returned" in l or l.startswith(("ERROR:", "FAIL:", "--- FAIL:")) or "defined and not used" in l or "undefined:" in l) and not any(w in l.lower() for w in ("token", "credential", "remote_header", "bes_header"))][:8]}


if __name__ == "__main__":
    root = Path(sys.argv[1])
    arms = {name: observe(root / name) for name in ["netstack"]}
    good = all(v["verified"] for v in arms.values()) and len({v.get("fixture") for v in arms.values()}) == 1
    (root / "verdict.json").write_text(json.dumps({"verified": good, "source": os.environ["CANDIDATE"], "arms": arms}, indent=2)+"\n")
    if len(sys.argv) == 2:
        raise SystemExit(not good)
