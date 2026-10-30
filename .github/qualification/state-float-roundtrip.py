import datetime
import json
import os
import re
import sys
from pathlib import Path

tmp = Path(os.environ["RUNNER_TEMP"])
code = int(sys.argv[1])
log = Path("bazel-testlogs/pkg/state/state_test/test.log")
rows = re.findall(r"BITS (legacy|graph) (scalar|complex|slice) ([0-9a-f]{8}) ([0-9a-f]{8})", log.read_text() if log.is_file() else "")
expected = {(engine, form, arg) for engine in ("legacy", "graph") for form in ("scalar", "complex", "slice")
            for arg in ("00000000", "80000000", "c1200000", "7fc01234", "7f801234", "ff801234")}
seen = {(e, f, x) for e, f, x, y in rows}
missing, extra = sorted(expected - seen), sorted(seen - expected)
complete = code == 0 and len(rows) == len(expected) and not missing and not extra
receipt = {"source": os.environ["GITHUB_SHA"], "upstream": "fbef70ac697e0b4c622daf548a0101b57c388894",
           "target": "//pkg/state:state_test", "native_exit": code, "expected_count": len(expected),
           "missing": missing, "unexpected": extra, "skips": [], "observed": rows,
           "mismatches": [v for v in rows if v[2] != v[3]], "execution_finished": (tmp/"end").read_text().strip(),
           "verified": datetime.datetime.now(datetime.timezone.utc).isoformat(), "status": "observed" if complete else "incomplete"}
(tmp/"probe-result.json").write_text(json.dumps(receipt, indent=2)+"\n")
sys.exit(code or (0 if complete else 1))
