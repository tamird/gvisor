"""Attribute the heap samples to allocation stacks under the original loadKernel."""
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path(sys.argv[1])
out = root / "records"
profile = root / "heap-report-input.pprof"
assert profile.is_file() and profile.stat().st_size > 0
results = {}
for metric in ("alloc_space", "inuse_space", "alloc_objects", "inuse_objects"):
    unit = "bytes" if metric.endswith("_space") else "count"
    flags = ["-sample_index=" + metric, "-unit=" + unit, "-nodefraction=0", r"-focus=[.]loadKernel$"]
    record = results[metric] = {}
    for name, options in [("stacks", ["-traces"]), ("top", ["-top", "-lines", "-nodecount=120"]),
                          ("cum", ["-top", "-cum", "-nodecount=90"])]:
        result = subprocess.run(["go", "tool", "pprof", *flags, *options, str(profile)],
                                text=True, capture_output=True)
        (out / (metric + "-" + name + ".txt")).write_text(result.stdout)
        if result.returncode:
            (out / (metric + "-" + name + "-error")).write_text(result.stderr[-1000:])
        record[name] = result.returncode == 0 and "Type: " + metric in result.stdout
        if name == "stacks":
            record[name] &= "loadKernel" in result.stdout and "/pkg/state." in result.stdout
    (out / "heap-report.json").write_text(json.dumps({"profiles": results, "at_ns": time.time_ns()}, indent=2))
assert all(all(record.values()) for record in results.values()), results
