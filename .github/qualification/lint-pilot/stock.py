"""Characterize the original downstream failure without publishing raw logs."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from consumer import verify

root = Path(os.environ["RUNNER_TEMP"])
stock = root / "stock-consumer.json"
expected = "0fa6803f2a5991dc066a29fc50414b4a3b519590"
if sys.argv[1:] == ["compare"]:
    observed = json.loads(stock.read_text())
    previous = json.loads((root / "original-launch.json").read_text())
    tags = {kind: sorted({tag for p in row.get("pages", []) for tag in p["tags"]})
            for kind, row in previous.items()}
    expected_tags = observed["downstream"]["forbidden_override_tags"]
    consistent = (
        len(expected_tags) == 1 and observed["downstream"]["characterized"]
        and observed["downstream"]["known_gazelle_failure"]
        and set(tags) == {"integrated_detection", "stock_waiter"}
        and all(value == expected_tags for value in tags.values())
    )
    observed.update(original_tags=tags, verdict="MATCH" if consistent else "INCOMPLETE",
                    verified_at=datetime.now(timezone.utc).isoformat())
    stock.write_text(json.dumps(observed, indent=2) + "\n")
    print(observed["verdict"], expected_tags, tags, flush=True)
    sys.exit(0 if consistent else 1)
sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert sha == expected
observed = {"source": sha, "verdict": "INCOMPLETE", "downstream": verify()}
stock.write_text(json.dumps(observed, indent=2) + "\n")
