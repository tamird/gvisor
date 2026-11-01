"""Measure one immutable checkpoint using the unmodified upstream benchmark."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys
import time

root, binary = map(Path, sys.argv[1:])
checkpoint = root / "capture"
out = root / "records"
private = root / "private"
private.mkdir(exist_ok=True)
cpus = sorted(os.sched_getaffinity(0))
env = dict(os.environ, GOMAXPROCS="1", GO_TEST_WRAP="0")
env.pop("XML_OUTPUT_FILE", None)
flags = ["taskset", "-c", str(cpus[0]), str(binary), "-checkpoint-dir=" + str(checkpoint), "-test.cpu=1"]
def files():
    result = {}
    for name in ("checkpoint.img", "pages.img", "pages_meta.img", "spec.json"):
        p = checkpoint / name
        result[name] = {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        assert result[name]["bytes"] > 0, name
    return result
def save(name, obj):
    (out/name).write_text(json.dumps(obj, indent=2) + "\n")
baseline = files()
save("capture.json",baseline)
def run(args, extra_env):
    p = subprocess.run(flags + args, env={**env, **extra_env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,text=True, timeout=240)
    if p.returncode or not re.search(r"^PASS$", p.stdout, re.M):
        raise RuntimeError(p.stdout[-3500:])
    return p.stdout
def bench(count, extra, extra_env):
    mode = "DecodeGC"
    output=run(["-test.run=^$","-test.bench=^BenchmarkCheckpointFile$/^"+mode+"$","-test.benchtime="+str(count)+"x", "-test.benchmem", *extra], extra_env)
    line, = [x for x in output.splitlines() if re.match("^BenchmarkCheckpointFile/"+mode+r"(-\d+)?\s", x)]
    name, n, *values = line.split()
    assert int(n) == count and len(values)%2==0, line
    row={k: float(v) for v,k in zip(values[::2],values[1::2])}
    assert all(row.get(k,0)>0 for k in ("ns/op","B/op","allocs/op","cpu-ns")), line
    assert row["heap-live-B"] > 0 and row["gcs/op"] > 0
    return row

# Keep ordinary CPU sampling independent of the finer allocation sampling.
cpu_profile = private / "decode.cpu"
save("cpu-run.json", bench(24, ["-test.cpuprofile=" + str(cpu_profile)], {}))
assert files() == baseline
live_profile = private / "kernel-live.heap"
save("heap-run.json", bench(1, ["-test.memprofilerate=16384"],
                            {"CHECKPOINT_PROFILE_LIVE_HEAP": str(live_profile)}))
assert files() == baseline
save("profiles.json", {"cpu_bytes": cpu_profile.stat().st_size, "heap_bytes": live_profile.stat().st_size,
                       "alloc_sample_rate": 16384, "cpu": cpus[0], "cpu_benchmark_n": 24,
                       "single_live_heap": True, "time_ns": time.time_ns()})
