"""Recover the two original test verdicts without running either test again."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from case_records import case_records

archive, output = map(Path,sys.argv[1:])
digest = "6aa2e1d64ba196c073fdbe6b85d7756b72436e7f0d2d09a67f15556d7291cca7"
assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
with ZipFile(archive) as z:
    old = json.loads(z.read("verdict.json"))
    source = "498afee3167561288981074241ff68f562a096a1"
    assert old["sources"] == {"candidate":source} and not old["failures"]
    arms = {}
    for name, expected in {
        "candidate-wire":["TestRACKRepeatedSACKAfterTimeout","TestRACKRepeatedSACKAfterTimeout/repeat_high","TestRACKRepeatedSACKAfterTimeout/repeat_low"],
        "candidate-positive":["TestRACKRepeatedLowerSACK"],
    }.items():
        arm = json.loads(z.read(f"{name}/arm.json"))
        assert arm == old["arms"][name] and arm["verified"] and arm["source"] == source
        assert arm["exit"] == 0 and int(z.read(f"{name}/exit")) == 0
        report = case_records(z.read(f"{name}/test.log"), expected)
        assert report["verified"], report
        arms[name] = dict(cases=report, invocation=arm["invocation"], ended=arm["ended"])
    assert set(arms) == set(old["expected"])
    w = old["arms"]["candidate-wire"]
    assert w["repeated_high_low"] == [0,0]
    tag = w["reneged_segment"]["ack_frames"]
    sends = w["reneged_segment"]["pcap_frames"]
    assert tag["3"] < tag["11"] < tag["12"] and not any(tag["3"]<i<tag["11"] for i in sends) and any(tag["11"]<i<tag["12"] for i in sends)
    assert [r[1] for r in old["arms"]["candidate-positive"]["genuine_reorder"]] == [0,0,1]
    for path, want in {
        "candidate-wire/trace-7cd2229fa.pcap":"7cd2229fa7ecb9364280edeb7d1d8792ff5df58b873a699ba1e88a69e8d6ead4",
        "candidate-positive/trace.pcap":"574572f887f49a4ad3e2ece0c0868b23ed45d470c4b213aedf6b7e145cdd09a8",
    }.items():
        assert hashlib.sha256(z.read(path)).hexdigest() == want
    verdict = dict(original_run=41189379538, original_artifact=13038417018, archive_sha256=digest,
        source=source, arms=arms, execution_finished_at=old["execution_finished_at"],
        original_verdict_at=old["verdict_available_at"], recovered_at=datetime.now(timezone.utc).isoformat(),
        missing=[],skipped=[],failures=[],verified=True)
    output.write_text(json.dumps(verdict,indent=2)+"\n")
