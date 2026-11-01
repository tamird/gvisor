"""Collect symbolized stacks from the controlled benchmark process alone."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
root=Path(sys.argv[1]);out=root/'records'
def pprof(src,*args):
    run = subprocess.run(["go", "tool", "pprof", *args, str(src)],text=True,capture_output=True)
    if run.returncode:
        raise RuntimeError(run.stderr[-1000:])
    return run.stdout
receipts = {}
for metric, source, unit in (("cpu", "decode.cpu", "ms"), ("alloc_space", "kernel-live.heap", "bytes"),
                             ("inuse_space", "kernel-live.heap", "bytes"), ("alloc_objects", "kernel-live.heap", "count"),
                             ("inuse_objects", "kernel-live.heap", "count")):
    profile = root / "private" / source
    flags = ["-sample_index=" + metric, "-unit=" + unit, "-nodefraction=0"]
    # The unfiltered CPU report also accounts for GC running outside loadKernel.
    if metric == "cpu":
        (out / "cpu-whole-top.txt").write_text(pprof(profile, *flags, "-top", "-nodecount=80"))
    flags.append(r"-focus=[.]loadKernel$")
    traces = pprof(profile, *flags, "-traces")
    assert "loadKernel" in traces and "/pkg/state." in traces and "Type: " + metric in traces, metric
    (out / (metric + "-stacks.txt")).write_text(traces)
    (out / (metric + "-top.txt")).write_text(pprof(profile, *flags, "-top", "-lines", "-nodecount=120"))
    (out / (metric + "-cum.txt")).write_text(pprof(profile, *flags, "-top", "-cum", "-nodecount=90"))
    receipts[metric] = {"bytes": profile.stat().st_size, "scope": "loadKernel", "sample_index": metric}
    (out / "symbol-verdict.json").write_text(json.dumps({"profiles": receipts, "at_ns": time.time_ns()}, indent=2) + "\n")
assert len(receipts) == 5
