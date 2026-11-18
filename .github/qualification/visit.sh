#!/bin/bash
set -euo pipefail
w="$1"; results="$2"; support="$3"; arm="$4"; kind="$5"; number="$6"
active="$w/active"
prefix="$kind-$number-$arm"
test -z "$(timeout 20s sudo docker ps -aq)"
timeout 30s sudo python3 "$support/quiescent.py" "$active"
rm -rf -- "$active"
cp -a "$w/$arm/release" "$active"
python3 "$support/fileset.py" verify "$active" "$results/$arm/frozen-fileset.json" "$results/$prefix-active-before.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$prefix-harness-before.json"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/iperf:latest > "$results/$prefix-image-before.txt"
cmp "$results/image-identity.txt" "$results/$prefix-image-before.txt"
sudo python3 - "$active/runsc" <<'CONFIG'
import json
from pathlib import Path
import sys
actual=json.loads(Path('/etc/docker/daemon.json').read_text())['runtimes']['systrap']
assert actual['path']==sys.argv[1],actual
assert actual.get('runtimeArgs')==['--platform=systrap','--network=sandbox','--debug=false'],actual
CONFIG
mkdir -p "$w/bench-tmp" "$results/$prefix-output"
runfiles="$w/harness/iperf_test.runfiles"
start=$(date +%s)
timeout --signal=TERM --kill-after=10s 150s sudo env RUNTIME=systrap GOMAXPROCS=4 RUNFILES_DIR="$runfiles" TEST_SRCDIR="$runfiles" TEST_WORKSPACE=_main TMPDIR="$w/bench-tmp" TEST_TMPDIR="$w/bench-tmp" TEST_UNDECLARED_OUTPUTS_DIR="$results/$prefix-output" "$w/harness/iperf_test" -runtime=systrap -test.run=NoTests -test.bench=Benchmark -test.benchtime=1048576x -test.count=1 -test.cpu=4 -test.timeout=140s 2>&1 | tee "$results/$prefix.log"
printf '%s\t%s\n' "$prefix" "$(( $(date +%s)-start ))" >> "$results/visit-seconds.tsv"
test -z "$(timeout 20s sudo docker ps -aq)"
timeout 30s sudo python3 "$support/quiescent.py" "$active"
python3 "$support/fileset.py" verify "$active" "$results/$arm/frozen-fileset.json" "$results/$prefix-active-after.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$prefix-harness-after.json"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/iperf:latest > "$results/$prefix-image-after.txt"
cmp "$results/image-identity.txt" "$results/$prefix-image-after.txt"
