from datetime import datetime, timezone
import json, os, re, sys
from pathlib import Path

root = Path(sys.argv[1])
arms = {"stock": os.environ["STOCK"], "candidate": os.environ["FIXED"]}
names = {f"BenchmarkBulkBatchedLocalhost/batchWindow={b}/cc=cubic/rack={v}" for b in ("0s", "100µs") for v in ("true", "false")}
exits = (root/"exits").read_text().splitlines() if (root/"exits").exists() else []
errors, observations = [], []
for arm, src in arms.items():
    series = []
    for i in range(13):
        path = root/f"{arm}-{i}.log"
        if not path.exists() or f"{arm} {i} {src} 0" not in exits:
            errors.append([arm, i, "no successful source-bound action"]); continue
        rows = re.findall(r"transfer name=(\S+) iterations=1 iteration=0 bytes=67108864 elapsed_ns=(\d+) retransmits=(\d+) recoveries=(\d+)", path.read_text())
        if len(rows) != 4 or {x[0] for x in rows} != names:
            errors.append([arm, i, "missing or repeated transfer", [x[0] for x in rows]]); continue
        for name, ns, retransmits, recoveries in rows:
            observations.append([arm,i,name,int(ns),int(retransmits),int(recoveries)])
            if i: series.append(f"{name} 1 {ns} ns/op {retransmits} retransmits/op {recoveries} recoveries/op")
    (root/f"{arm}.bench").write_text("\n".join(series)+"\n")
end = (root/"execution-end").read_text().strip() if (root/"execution-end").exists() else None
(root/"verdict.json").write_text(json.dumps(dict(sources=arms, expected=104, executed=len(observations), errors=errors, passed=not errors and len(observations)==104, execution_finished=end, verified=datetime.now(timezone.utc).isoformat(), observations=observations)))
sys.exit(bool(errors) or len(observations) != 104)
