"""Read original TCP packet observations and verify the paired outcome."""
import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree

records = Path(os.environ["RUNNER_TEMP"]) / "records"
verdict = {"verified": False, "arms": {}}
heads = {"review": "47d4d443c0801c2cc6981ccebec8901f4ca758d1",
         "serial": "f6a760096dbb5003ee408e784b1c4c015519100f",
         "episode": "1c3a16f772aae1386fe0baa10940257957e6d3c8"}
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
    for arm, head in heads.items():
        d = records / arm
        observed = verdict["arms"][arm]["rows"]
        assert (d / "head").read_text().strip() == head
        assert len(observed) == 8, observed
        xml = ElementTree.parse(d / "test.xml")
        assert not list(xml.iter("skipped")), "skipped test"
        assert int((d / "exit").read_text()) == (0 if arm == "episode" else 3)
        for key, row in observed.items():
            wrap, copy = key.split("/")
            want_undo = copy in {"original", "late-original"}
            if arm == "review" and (wrap == "true" and copy == "original" or copy == "late-original"):
                want_undo = False
            if arm == "serial" and copy == "late-repair":
                want_undo = True
            assert row["spurious"] == int(want_undo), (arm, key, row)
            assert (row["threshold"] == 10) == want_undo, (arm, key, row)
        assert (d / "status").read_text().strip() == ("PASSED" if arm == "episode" else "FAILED")
    assert len({(records / arm / "launcher-rule-blob").read_text() for arm in heads}) == 1
    verdict["verified"] = True
finally:
    (records / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
