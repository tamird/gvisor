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
paths = [str(test.with_name(name)) for name in ("endpoint.go", "endpoint_state.go", "rcv.go")]
assert subprocess.check_output(
    ["git", "diff", "--name-only", stock, product, "--", str(test.parent)], text=True
).splitlines() == paths

# A failed local request must not manufacture RcvClosed on subsequent connect.
if os.environ.get("READINESS_CONTROL") == "1":
    original = test.read_text()
    old_build = build.read_text()
    try:
        test.write_text(original.replace('"testing"', '"context"\n\t"testing"\n\t"gvisor.dev/gvisor/pkg/tcpip"\n\t"gvisor.dev/gvisor/pkg/waiter"') + """
func TestRejectedShutdownReadiness(t *testing.T) {
	var ep Endpoint
	ep.state.Store(uint32(StateInitial))
	if err := ep.Shutdown(tcpip.ShutdownRead); err == nil {
		t.Fatal("shutdown succeeded on an unconnected endpoint")
	} else if _, ok := err.(*tcpip.ErrNotConnected); !ok {
		t.Fatalf("shutdown error = %T, want ErrNotConnected", err)
	}
	if got := ep.saveShutdownFlags(); got != tcpip.ShutdownRead {
		t.Fatalf("saved shutdown flags = %d, want ShutdownRead", got)
	}
	var restored Endpoint
	restored.loadShutdownFlags(context.Background(), ep.saveShutdownFlags())
	for _, e := range []*Endpoint{&ep, &restored} {
		e.state.Store(uint32(StateEstablished))
		if got := e.Readiness(waiter.EventRdHUp); got != 0 {
			t.Fatalf("Readiness(EventRdHUp) = %v after rejected shutdown", got)
		}
	}
}
""")
        prefix, rule = old_build.split('name = "tcp_test",', 1)
        build.write_text(prefix + 'name = "tcp_test",' + rule.replace('"//pkg/sync",', '"//pkg/sync",\n        "//pkg/waiter",', 1))
        run = subprocess.run(["docker", "exec", "tcp-compiler", "bazel", "--bazelrc=" + str(root / "tcp.bazelrc"),
                              "test", "--config=x86_64", "--jobs=40", "--strategy=TestRunner=local",
                              "--nocache_test_results", "--test_output=all", "--test_sharding_strategy=disabled",
                              "--test_arg=-test.v", "--test_arg=-test.run=^TestRejectedShutdownReadiness$",
                              "//pkg/tcpip/transport/tcp:tcp_test"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        passed = run.returncode == 0 and "--- PASS: TestRejectedShutdownReadiness " in run.stdout
        receipt = {"source": product, "exit": run.returncode, "case": "TestRejectedShutdownReadiness",
                   "verified": passed, "invocations": list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})", run.stdout)))}
        (out / "preconnect.json").write_text(json.dumps(receipt) + "\n")
        assert passed, receipt
    finally:
        test.write_text(original)
        build.write_text(old_build)
    raise SystemExit(0)

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
assert fixture == "885b5725abdb49555254c4cc350ac440fb41a973e3914b73536d4319bf9504bb"
assert stock == "254c8ddfcc97180f83ba6454db22afb6235b5df3"

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
