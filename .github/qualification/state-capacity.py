import datetime
import json
import os
import re
import sys
from pathlib import Path

tmp = Path(os.environ["RUNNER_TEMP"])
code = int(sys.argv[1])
log = Path("bazel-testlogs/pkg/state/state_test/test.log")
found = [(int(c), int(n), status) for c, n, status in
         re.findall(r"CAPACITY (\d+) BYTES (\d+) RESULT (round-trip|rejected-capacity)", log.read_text() if log.is_file() else "")]
expected = [7, 1 << 40, (1 << 40) + 1]
missing = [c for c in expected if c not in {v[0] for v in found}]
good = code == 0 and not missing and len(found) == len(expected)
receipt = {"source": os.environ["GITHUB_SHA"], "upstream": "fbef70ac697e0b4c622daf548a0101b57c388894",
           "target": "//pkg/state:state_test", "native_exit": code, "expected_capacities": expected,
           "observed": found, "missing": missing, "execution_finished": (tmp/"end").read_text().strip(),
           "verified": datetime.datetime.now(datetime.timezone.utc).isoformat(), "status": "observed" if good else "incomplete"}
(tmp/"probe-result.json").write_text(json.dumps(receipt, indent=2)+"\n")
sys.exit(code or (0 if good else 1))
