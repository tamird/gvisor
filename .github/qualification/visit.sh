#!/bin/bash
set -euo pipefail
w="$1"; results="$2"; support="$3"; side="$4"; kind="$5"; pair="$6"
prefix="$kind-$pair-$side"
active="$w/active"
test -z "$(timeout 15s sudo docker ps -aq)"
timeout 15s sudo python3 "$support/quiescent.py" "$active"
rm -rf -- "$active"
cp -a "$w/$side/runtime" "$active"
python3 "$support/fileset.py" verify "$active" "$results/$side-frozen-runtime.json" "$results/$prefix-runtime-before.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$prefix-harness-before.json"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/hackbench:latest > "$results/$prefix-images-before.txt"
cmp "$results/frozen-images.txt" "$results/$prefix-images-before.txt"
sudo python3 - "$active/runsc" <<'CONFIG'
import json
from pathlib import Path
import sys
actual = json.loads(Path('/etc/docker/daemon.json').read_text())['runtimes']['systrap']
assert actual['path'] == sys.argv[1], actual
assert actual.get('runtimeArgs') == ['--platform=systrap', '--network=sandbox', '--debug=false'], actual
CONFIG
mkdir -p "$w/bench-tmp" "$results/$prefix-output"
for suite in startup hackbench; do
 binary=startup_test; selector=BenchmarkStartupEmpty; benchtime=1x; cap=15s
 if test "$suite" = hackbench; then binary=hackbench_test; selector=BenchmarkHackbench; benchtime=20x; cap=50s; fi
 runfiles="$w/harness/$binary.runfiles"
 timeout --signal=TERM --kill-after=5s "$cap" sudo env RUNTIME=systrap GOMAXPROCS=4 RUNFILES_DIR="$runfiles" TEST_SRCDIR="$runfiles" TEST_WORKSPACE=_main TMPDIR="$w/bench-tmp" TEST_TMPDIR="$w/bench-tmp" TEST_UNDECLARED_OUTPUTS_DIR="$results/$prefix-output" "$w/harness/$binary" -runtime=systrap -test.run=NoTests "-test.bench=$selector" "-test.benchtime=$benchtime" -test.count=1 -test.cpu=4 2>&1 | tee "$results/$prefix-$suite.log"
done
test -z "$(timeout 15s sudo docker ps -aq)"
timeout 15s sudo python3 "$support/quiescent.py" "$active"
python3 "$support/fileset.py" verify "$active" "$results/$side-frozen-runtime.json" "$results/$prefix-runtime-after.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$prefix-harness-after.json"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/hackbench:latest > "$results/$prefix-images-after.txt"
cmp "$results/frozen-images.txt" "$results/$prefix-images-after.txt"
