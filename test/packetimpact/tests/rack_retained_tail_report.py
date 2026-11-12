"""Check observations against the measured first feedback on each test socket."""

import json
import re
import sys
from pathlib import Path


def fields(line):
    return dict(re.findall(r"(\w+)=([^\s]+)", line))


def check_arm(geometry, rounds, trace):
    base = geometry["base"]
    port = geometry["sport"]
    terminal = int(geometry["terminal"])
    assert [geometry[key] for key in ("flight", "mss", "queued", "window", "lower", "upper")] == [
        "40", "536", "4", "65535", "6", "30",
    ]
    assert int(geometry["middle"]) == 4 - terminal
    rows = [row for row in trace if row["port"] == port and row["base"] == base]
    assert [row["phase"] for row in rows] == ["detected", "resent"], rows
    detected, resent = rows
    middle = range(6, 6 + 4 - terminal)
    tail = range(40 - terminal, 40)
    received = sum(1 << i for i in (*middle, *tail))
    expected_visited = received
    for row in rows:
        assert row["originals"] == "40" and row["separate"] == "true", row
        assert int(row["received"], 16) == received
        assert int(row["visited"], 16) == expected_visited
    assert int(detected["credit"]) == expected_visited.bit_count()
    assert resent["credit"] == "0" and resent["high_rxt"] == "535"
    assert int(resent["timed"], 16) == int(detected["timed"], 16) & ~1
    charged = int(resent["charged"], 16)
    score_lost = int(resent["scoreboard"], 16)
    rack_lost = int(resent["timed"], 16)
    assert (received | score_lost | charged) == (1 << 40) - 1
    assert not (charged & (received | score_lost) or received & score_lost)
    scoreboard_flight = charged.bit_count() + 1
    assert scoreboard_flight == (31 if terminal == 1 else 1)
    observed_flight = int(resent["pipe"])
    assert observed_flight == scoreboard_flight - (charged & rack_lost).bit_count()
    assert resent["cwnd"] == "31"
    first = rounds[0]
    assert all(fields(row)["rtos"] == "0" and fields(row)["backoff"] == "0" for row in rounds)
    emitted = list(map(int, re.search(r"sent=\[([^\]]*)\]", first).group(1).split()))
    assert all(0 <= i < 44 for i in emitted), emitted
    repairs = [i for i in emitted if i < 40]
    fresh = [i for i in emitted if i >= 40]
    return {
        "variant": "combined", "terminal": terminal, "socket_port": port, "base": base,
        "observations": rows, "first_wire_segments": emitted,
        "actual_pipe_after_first_resend": observed_flight,
        "scoreboard_only_pipe_after_first_resend": scoreboard_flight,
        "first_wire_repair_bytes": 536 * len(repairs), "first_wire_fresh_bytes": 536 * len(fresh),
        "time_marked_excluded_from_flight": [i for i in range(40) if charged & rack_lost & (1 << i)],
    }


def main():
    root = Path(sys.argv[1])
    text = (root / "test.log").read_text()
    cases = text.split("GEOMETRY base=")[1:]
    assert len(cases) == 2, len(cases)
    boots = list((root / "outputs").glob("*boot*.log"))
    assert len(boots) == 1, boots
    trace = [fields(line) for line in boots[0].read_text().splitlines() if "RACKTAIL phase=" in line]
    output = []
    for case in cases:
        geometry = fields("base=" + case.splitlines()[0])
        rounds = [line for line in case.splitlines() if "ROUND n=" in line]
        assert len(rounds) == 5
        output.append(check_arm(geometry, rounds, trace))
    assert [row["terminal"] for row in output] == [1, 3]
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
