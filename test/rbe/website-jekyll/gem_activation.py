import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
records = []
for line in (root / "bazel.log").read_text().splitlines():
    if "GVISOR_GEMS " in line:
        records.append(json.loads(line.split("GVISOR_GEMS ", 1)[1]))
(root / "activation.json").write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
print([record["program"] for record in records])
