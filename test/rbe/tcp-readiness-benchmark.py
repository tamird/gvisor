"""Compare the original and corrected Readiness implementation on one worker."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess

root = Path(os.environ["RUNNER_TEMP"])
out = root / "tcp-evidence"
product = os.environ["PRODUCT"]
stock = subprocess.check_output(["git", "rev-parse", product + "^"], text=True).strip()
test = Path("pkg/tcpip/transport/tcp/endpoint_test.go")
build = test.with_name("BUILD")
paths = [str(test.with_name(name)) for name in ("endpoint.go", "rcv.go")]
assert subprocess.check_output(
    ["git", "diff", "--name-only", stock, product, "--", str(test.parent)], text=True
).splitlines() == paths

test.write_text(test.read_text().replace('"gvisor.dev/gvisor/pkg/sync"', '"gvisor.dev/gvisor/pkg/sync"\n\t"gvisor.dev/gvisor/pkg/waiter"') + """
func BenchmarkEstablishedReadiness(b *testing.B) {
	var ep Endpoint
	ep.state.Store(uint32(StateEstablished))
	ep.ops.SetSendBufferSize(65536, false)
	for _, tc := range []struct {
		name string
		mask, want waiter.EventMask
	}{
		{"Read", waiter.ReadableEvents, 0},
		{"Write", waiter.WritableEvents, waiter.WritableEvents},
		{"ReadWrite", waiter.ReadableEvents | waiter.WritableEvents, waiter.WritableEvents},
		{"ReadHangup", waiter.EventRdHUp, 0},
		{"WriteReadHangup", waiter.WritableEvents | waiter.EventRdHUp, waiter.WritableEvents},
	} {
		b.Run(tc.name, func(b *testing.B) {
			if got, want := ep.Readiness(tc.mask), tc.want; got != want {
				b.Fatalf("Readiness(%#x) = %#x, want %#x", tc.mask, got, want)
			}
			for b.Loop() {
				ep.Readiness(tc.mask)
			}
		})
	}
}
""")
prefix, rule = build.read_text().split('name = "tcp_test",', 1)
build.write_text(prefix + 'name = "tcp_test",' + rule.replace('"//pkg/sync",', '"//pkg/sync",\n        "//pkg/waiter",', 1))
fixture = hashlib.sha256(test.read_bytes()).hexdigest()
sources = {"stock": stock, "fixed": product}
samples = {arm: {} for arm in sources}
pairs = 12
now = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
receipt = {"fixture": fixture, "sources": sources, "expected_pairs": pairs,
           "samples_ns": samples, "verified": False, "started": now()}
report = out / "benchmark.json"
try:
    for pair in range(pairs):
        for arm in (("fixed", "stock") if pair % 2 else ("stock", "fixed")):
            subprocess.run(["git", "restore", "--source=" + sources[arm], "--", *paths], check=True)
            subprocess.run(["git", "diff", "--quiet", sources[arm], "--", *paths], check=True)
            command = [
                "docker", "exec", "tcp-compiler", "bazel", "--bazelrc=" + str(root / "tcp.bazelrc"),
                "test", "--config=x86_64", "--jobs=40", "--strategy=TestRunner=local",
                "--nocache_test_results", "--test_output=all", "--test_sharding_strategy=disabled",
                "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkEstablishedReadiness$",
                "--test_arg=-test.benchtime=200ms", "--test_arg=-test.cpu=1",
                "//pkg/tcpip/transport/tcp:tcp_test",
            ]
            run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if run.returncode:
                for line in run.stdout.splitlines():
                    if "error" in line.lower() and not any(term in line.lower() for term in ("credential", "token", "header", "key=")):
                        print(line[:300], flush=True)
                run.check_returncode()
            rows = {}
            raw = []
            for line in run.stdout.splitlines():
                match = re.fullmatch(r"BenchmarkEstablishedReadiness/(Read|Write|ReadWrite|ReadHangup|WriteReadHangup)(?:-1)?\s+\d+\s+([0-9.]+) ns/op(?:\s+.*)?", line.strip())
                if match:
                    name, value = match.groups()
                    assert name not in rows, name
                    rows[name] = float(value)
                    raw.append(line.strip())
            assert set(rows) == {"Read", "Write", "ReadWrite", "ReadHangup", "WriteReadHangup"}, sorted(rows)
            with (out / (arm + ".bench")).open("a") as log:
                print(*raw, sep="\n", file=log)
            for name, value in rows.items():
                samples[arm].setdefault(name, []).append(value)
            report.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt["median_ns"] = {
        arm: {name: statistics.median(values) for name, values in modes.items()}
        for arm, modes in samples.items()
    }
    receipt["verified"] = all(len(values) == pairs for modes in samples.values() for values in modes.values())
finally:
    receipt["finished"] = now()
    report.write_text(json.dumps(receipt, indent=2) + "\n")
assert receipt["verified"]
