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
modes = ["EncodeStatefile", "DecodeStatefile", "DecodeGC"]
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
def run(args):
    p = subprocess.run(flags + args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,text=True, timeout=240)
    if p.returncode or not re.search(r"^PASS$", p.stdout, re.M):
        raise RuntimeError(p.stdout[-3500:])
    return p.stdout
def bench(mode,count,extra=()):
    output=run(["-test.run=^$","-test.bench=^BenchmarkCheckpointFile$/^"+mode+"$","-test.benchtime="+str(count)+"x", "-test.benchmem", *extra])
    line, = [x for x in output.splitlines() if re.match("^BenchmarkCheckpointFile/"+mode+r"(-\d+)?\s", x)]
    name, n, *values = line.split()
    assert int(n) == count and len(values)%2==0, line
    row={k: float(v) for v,k in zip(values[::2],values[1::2])}
    assert all(row.get(k,0)>0 for k in ("ns/op","B/op","allocs/op","cpu-ns")), line
    if mode=="EncodeStatefile":
        assert row["encoded-kernel-B"]>0
    elif mode=="DecodeStatefile":
        assert row["state-file-B"]==baseline["checkpoint.img"]["bytes"] and row["pages-file-B"]==baseline["pages.img"]["bytes"]
    return row
for mode in modes:
    profile=private/(mode+".pprof")
    single=bench(mode,12,["-test.cpuprofile="+str(profile)])
    save("profile-"+mode+".json",single)
assert files()==baseline
save("profile-only.json",{"fresh_capture":baseline["checkpoint.img"],"iterations_per_mode":12,"timing_samples_repeated":0,"measurement_cpu":cpus[0]})
