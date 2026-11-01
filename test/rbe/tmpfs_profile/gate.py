"""Stop before the kernel comparison if a material scalar/sparse regression persists."""
import json
import os
from pathlib import Path
from statistics import median

folder = Path(os.environ["RUNNER_TEMP"]) / "small/records"
rows = json.loads((folder/"paired-bench.json").read_text())
assert len(rows) == 288, len(rows)
results = []
for mode, name in sorted({(r["mode"], r["name"]) for r in rows}):
    for metric in ("B/op", "ns/op"):
        samples = {(r["arm"], r["pair"]):r["metrics"][metric] for r in rows if r["mode"] == mode and r["name"] == name}
        assert len(samples) == 32, (mode, name, metric)
        old = [samples["stock", i] for i in range(16)]
        new = [samples["fixed", i] for i in range(16)]
        before, after = median(old), median(new)
        # A 5% loss must repeat in >=13 of 16 matched pairs. Allow at most
        # eight extra bytes on a tiny graph without disqualifying the design.
        floor = 8 if metric == "B/op" else 0
        limit = max(floor, .05 * before)
        bad = after-before > limit and sum((n-o > max(floor, .05*o)) for o,n in zip(old,new)) >= 13
        results.append({"mode":mode,"shape":name,"metric":metric,"old":before,"new":after,"material_regression":bad})
passed = not any(x["material_regression"] for x in results)
(folder/"gate.json").write_text(json.dumps({"open":passed,"rows":results},indent=2)+"\n")
with Path(os.environ["GITHUB_OUTPUT"]).open("a") as status:
    status.write("open="+str(passed).lower()+"\n")
