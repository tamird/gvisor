"""Run both receivers in alternating order on one Linux test worker."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

out = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
root = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rbe/paired"
names = ["reviewed", "bounded"]
sizes = [100, 536, 1448]
capture = re.compile(r"GOODPUT mss=(\d+) bytes=(\d+) ns=(\d+) dropped=(\d+) silence=(\d+) recovery=(\d+) zero_windows=(\d+)")
samples = []
start = time.monotonic()
result = {
    "status": "INCOMPLETE", "expected": 60, "samples": samples,
    "platform": os.uname().machine, "elapsed_includes_priming_byte": False,
    "sources": json.loads((root / "sources.json").read_text()),
}

def persist():
    (out / "verdict.json").write_text(json.dumps(result, indent=2) + "\n")

try:
    for name in names:
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        assert digest == result["sources"][name]["executable"], (name, digest)
    for pair in range(10):
        for name in (names if pair % 2 == 0 else names[::-1]):
            run = subprocess.run([root / name, "-test.run=^TestBurstGoodput$", "-test.v", "-test.timeout=135s"],
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=145)
            (out / f"{pair}-{name}.txt").write_text(run.stdout)
            assert run.returncode == 0, (pair, name, run.returncode, run.stdout[-1200:])
            matches = [tuple(map(int, row)) for row in capture.findall(run.stdout)]
            assert [row[0] for row in matches] == sizes, (pair, name, matches)
            assert run.stdout.count("--- PASS: TestBurstGoodput/") == len(sizes)
            for mss, amount, ns, drops, silence, retries, zero in matches:
                assert amount == (1 << 20) - 1 and ns > 0
                samples.append(dict(pair=pair, name=name, mss=mss, bytes=amount, ns=ns,
                                    dropped=drops, silence=silence, recovery=retries, zero_windows=zero))
            persist()
    for name in names:
        output = [f"BenchmarkReceivePressure/MSS={r['mss']} 1 {r['ns']} ns/op {r['dropped']} queue-rejects/op {r['silence']} timeouts/op"
                  for r in samples if r["name"] == name]
        (out / f"{name}.bench").write_text("\n".join(output) + "\n")
    times = {(r["mss"], r["pair"], r["name"]): r["ns"] for r in samples}
    result["median_paired_time_ratio"] = {
        size: statistics.median(times[size, pair, "bounded"] / times[size, pair, "reviewed"]
                                for pair in range(10)) for size in sizes
    }
    result["status"] = "PASS"
except Exception as err:
    result["failure"] = repr(err)
finally:
    result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result["duration_seconds"] = time.monotonic() - start
    persist()
print(result["status"], len(samples), result.get("failure", ""), flush=True)
sys.exit(result["status"] != "PASS")
