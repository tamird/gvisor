"""Measure the existing GC-enabled kernel decoder without allocation profiling."""
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import time


sys.argv.append("helpers")
s = runpy.run_path(str(Path(__file__).with_name("pair-run.py")))
root, out, gnu, bz, opts = (s[k] for k in ("root", "out", "gnu", "bz", "opts"))
capture = root / "live-00-fixed"
frozen = json.loads((out / "frozen.json").read_text())
label = "//test/benchmarks/checkpointfile:checkpointfile_test"
bench = "test/benchmarks/checkpointfile/checkpointfile.go"
blob = "588cc13ed911b4a04f5d192032a5afa315be61a0"
fields = {"ns/op", "cpu-ns", "gc-pause-ns/op", "gcs/op", "heap-live-B", "heap-peak-B", "B/op", "allocs/op"}
arms = {"stock": os.environ["BASE"], "fixed": os.environ["PROPOSAL"]}


def digest(exe):
    return subprocess.check_output(gnu + ["sha256sum", str(exe)], text=True).split()[0]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


cpu = int(subprocess.check_output(gnu + ["python3", "-c", "import os;print(min(os.sched_getaffinity(0)))"]))
info = {"status": "INCOMPLETE", "sources": arms, "host_cpu": cpu, "benchmark_blob": blob,
        "frozen": frozen, "binaries": {}, "attempts": [], "prior_profile": {
            "run": 41422456146, "artifact": 13131506474,
            "sha256": "28d6491e32f6a9eb6841f1b10e5267faea33b596f9b24f94639507a7e889676a"}}
s["record"]("paired.json", info)
for arm, source in arms.items():
    git("checkout", "-q", "--detach", source)
    assert git("rev-parse", "HEAD") == source and not git("diff", "--name-only", "HEAD")
    assert git("rev-parse", "HEAD:" + bench) == blob
    s["command"](arm + "-build", bz + ["build", *opts, label])
    exe = s["output_file"](arm + "-path", label)
    saved = root / arm
    subprocess.run(gnu + ["cp", str(exe), str(saved)], check=True)
    assert digest(exe) == digest(saved)
    info["binaries"][arm] = digest(saved)
    s["record"]("paired.json", info)
assert len(set(info["binaries"].values())) == 2


def measure(pair, arm, warmup=False):
    name = ("warmup" if warmup else f"pair-{pair}") + "-" + arm
    exe = root / arm
    assert digest(exe) == info["binaries"][arm] and s["file_digests"](capture) == frozen
    args = [*gnu, "taskset", "-c", str(cpu), "env", "GO_TEST_WRAP=0", "GOMAXPROCS=1", "GOGC=100",
            str(exe), "-checkpoint-dir=" + str(capture), "-test.cpu=1", "-test.run=^$",
            "-test.bench=^BenchmarkCheckpointFile$/^DecodeGC$", "-test.benchtime=1x", "-test.benchmem"]
    row = {"pair": pair, "arm": arm, "warmup": warmup, "argv": args, "start_ns": time.time_ns()}
    info["attempts"].append(row)
    s["record"]("paired.json", info)
    text = s["command"](name, args, get_stdout=True)
    rows = re.findall(r"^BenchmarkCheckpointFile/DecodeGC(?:-1)?\s+1\s+[^\n]+", text, re.M)
    row.update(finished_ns=time.time_ns(), rows=rows)
    s["record"]("paired.json", info)
    assert len(rows) == 1 and re.search(r"^PASS$", text, re.M)
    tokens = rows[0].split()
    values = dict(zip(tokens[3::2], map(float, tokens[2::2]), strict=True))
    assert set(values) == fields and all(n > 0 for n in values.values()), values
    assert int(values["heap-peak-B"]) >= int(values["heap-live-B"])
    assert s["file_digests"](capture) == frozen
    if not warmup:
        with (out / (arm + ".bench")).open("a") as f:
            print(rows[0], file=f)


for arm in arms:
    measure(0, arm, warmup=True)
for i in range(8):
    for arm in s["order"](i):
        measure(i + 1, arm)
assert len(info["attempts"]) == 18 and all(len(r["rows"]) == 1 for r in info["attempts"])
s["benchstat"]("stock.bench", "fixed.bench", "benchstat.txt")
summary = (out / "benchstat.txt").read_text()
assert "CheckpointFile/DecodeGC" in summary and all(unit in summary for unit in ("sec/op", "B/op", "allocs/op", "heap-peak-B", "heap-live-B"))
info.update(status="PASS", verified_ns=time.time_ns())
s["record"]("paired.json", info)
