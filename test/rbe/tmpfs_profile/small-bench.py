"""Compare the existing small/sparse graph Save shapes on one native CPU."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

root = Path(sys.argv[1])
out = root / "records"
cpu = min(os.sched_getaffinity(0))
env = dict(os.environ, GOMAXPROCS="1", GO_TEST_WRAP="0")
env.pop("XML_OUTPUT_FILE", None)
patterns = (
    ("warm", "250ms", "^BenchmarkGraphSlabShapes$", ["GraphSlabShapes/" + n for n in ("Tiny", "Many", "Alternating")]),
    ("warm", "250ms", "^BenchmarkSparseIndexSave$", ["SparseIndexSave/" + n for n in ("1", "65536", "16777216")]),
    ("warm", "250ms", "^BenchmarkEncoding$/^ptr$/^state$", ["Encoding/ptr/state"]),
    ("cold", "1x", "^BenchmarkSparseIndexSave$/^1$", ["SparseIndexSave/1"]),
    ("cold", "1x", "^BenchmarkEncoding$/^ptr$/^state$", ["Encoding/ptr/state"]),
)
rows = []
for i in range(16):
    for arm in (("stock", "fixed") if i % 2 == 0 else ("fixed", "stock")):
        for mode, duration, pattern, wanted in patterns[::(1 if (i // 2) % 2 == 0 else -1)]:
            args = ["taskset", "-c", str(cpu), str(root / arm), "-test.cpu=1", "-test.run=^$", "-test.bench="+pattern, "-test.benchtime="+duration, "-test.benchmem"]
            run = subprocess.run(args, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            if run.returncode or not re.search(r"^PASS$", run.stdout, re.M):
                raise RuntimeError(run.stdout[-1300:])
            entries = {}
            for line in run.stdout.splitlines():
                match = re.match(r"^Benchmark([^\s]+)\s+(\d+)\s+", line)
                if not match:
                    continue
                name = re.sub(r"-1$", "", match.group(1))
                assert name not in entries, name
                fields = line.split()[2:]
                metrics = {unit:float(value) for value, unit in zip(fields[::2], fields[1::2])}
                assert metrics["ns/op"] > 0 and metrics["B/op"] >= 0 and metrics["allocs/op"] >= 0, line
                assert (int(match.group(2)) == 1 if mode == "cold" else int(match.group(2)) > 1),line
                entries[name] = {"pair":i, "arm":arm, "mode":mode, "name":name, "metrics":metrics,
                                 "line":line.replace("Benchmark","Benchmark"+mode.title()+"/",1)}
            assert set(entries) == set(wanted), (pattern, entries, wanted)
            rows.extend(entries.values())
            (out/"paired-bench.json").write_text(json.dumps(rows, indent=2)+"\n")
assert len(rows) == 16*2*9, len(rows)
(out/"bench-finished.json").write_text(json.dumps({"cpu":cpu,"rows":len(rows),"end":time.time_ns()})+"\n")
