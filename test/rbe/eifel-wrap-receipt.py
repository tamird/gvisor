"""Read original TCP packet observations and verify the paired outcome."""
import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree

records = Path(os.environ["RUNNER_TEMP"]) / "records"
verdict = {"verified": False, "arms": {}}
heads = {"review": "d3d6262cc176d6dfe02131e58f032cdc85c87665",
         "serial": "385c7126ae4a7f31b4f1ca45d6fba49945a28cb6"}
pattern = re.compile(r"EIFEL delivered=(\S+) wrap=(true|false) original=(\d+) first=(\d+) echo=(\d+) next=(\d+) spurious=(\d+) cwnd=(\d+) threshold=(\d+)")
try:
    for arm, head in heads.items():
        d = records / arm
        observed = {}
        for line in (d / "test.log").read_text().splitlines():
            if match := pattern.search(line):
                delivered, boundary, *values = match.groups()
                key = (boundary, delivered)
                assert key not in observed, key
                observed[key] = dict(zip(("original", "first", "echo", "next", "spurious", "cwnd", "threshold"), map(int, values), strict=True))
        report = {f"{boundary}/{copy}": value for (boundary, copy), value in observed.items()}
        verdict["arms"][arm] = {"source": (d / "head").read_text().strip(), "rows": report,
                                "uuid": (d / "uuid").read_text().strip()}
        assert (d / "head").read_text().strip() == head
        assert len(observed) == 6, observed
        xml = ElementTree.parse(d / "test.xml")
        assert not list(xml.iter("skipped")), "skipped test"
        assert int((d / "exit").read_text()) == (3 if arm == "review" else 0)
        for (wrap, copy), row in observed.items():
            want_undo = copy == "original"
            if arm == "review" and wrap == "true" and copy != "repair":
                want_undo = not want_undo
            assert row["spurious"] == int(want_undo), row
            assert (row["threshold"] == 10) == want_undo, row
        assert (d / "status").read_text().strip() == ("FAILED" if arm == "review" else "PASSED")
    verdict["verified"] = True
finally:
    (records / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
