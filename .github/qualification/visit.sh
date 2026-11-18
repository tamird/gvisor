#!/bin/bash
set -euo pipefail
w="$1"; results="$2"; support="$3"; arm="$4"; label="$5"; batch="$6"
active="$w/active"
prefix="$label-$batch"
test -z "$(timeout 20s sudo docker ps -aq)"
timeout 30s sudo python3 "$support/quiescent.py" "$active"
# Recreate the same real directory and copy the whole opaque release on EVERY
# label, including the two A/A labels. No label-specific executable path exists.
rm -rf "$active"
cp -a "$w/$arm/release" "$active"
python3 "$support/fileset.py" verify "$active" "$results/$arm/frozen-fileset.json" "$results/$prefix-active-before.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$prefix-harness-before.json"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/syscallbench:latest gvisor.dev/images/benchmarks/alpine:latest > "$results/$prefix-images-before.txt"
cmp "$results/image-identities.txt" "$results/$prefix-images-before.txt"
sudo python3 - "$active/runsc" <<'PY'
import json, sys
from pathlib import Path
actual=json.loads(Path('/etc/docker/daemon.json').read_text())['runtimes']['systrap']['path']
if actual != sys.argv[1]:
    raise SystemExit(f'Unexpected active runtime path: {actual}')
PY
mkdir -p "$w/bench-tmp" "$results/$prefix-profile"
date -u +%FT%TZ > "$results/$prefix-start.txt"
# Execute the exact static binaries built by the canonical benchmark_test rule.
# This is the direct-host execution path documented by that owning rule.
for suite in syscallbench startup; do
  binary=syscallbench_test; selector=BenchmarkSyscallbench; benchtime=1s; warmup_cap=90s; measured_cap=120s
  if test "$suite" = startup; then
    binary=startup_test; selector=BenchmarkStartupEmpty; benchtime=1x; warmup_cap=30s; measured_cap=60s
  fi
  runfiles="$w/harness/$binary.runfiles"
  for kind in warmup measured; do
    count=3; cap="$warmup_cap"
    if test "$kind" = measured; then count=6; cap="$measured_cap"; fi
    timeout --signal=TERM --kill-after=10s "$cap" sudo env RUNTIME=systrap GOMAXPROCS=4 RUNFILES_DIR="$runfiles" TEST_SRCDIR="$runfiles" TEST_WORKSPACE=_main TMPDIR="$w/bench-tmp" TEST_TMPDIR="$w/bench-tmp" TEST_UNDECLARED_OUTPUTS_DIR="$results/$prefix-profile" "$w/harness/$binary" -runtime=systrap '-test.run=^$' "-test.bench=$selector" "-test.benchtime=$benchtime" "-test.count=$count" -test.cpu=4 -test.v 2>&1 | tee "$results/$prefix-$suite-$kind.log"
  done
done
date -u +%FT%TZ > "$results/$prefix-end.txt"
test -z "$(timeout 20s sudo docker ps -aq)"
timeout 30s sudo python3 "$support/quiescent.py" "$active"
python3 "$support/fileset.py" verify "$active" "$results/$arm/frozen-fileset.json" "$results/$prefix-active-after.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$prefix-harness-after.json"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/syscallbench:latest gvisor.dev/images/benchmarks/alpine:latest > "$results/$prefix-images-after.txt"
cmp "$results/image-identities.txt" "$results/$prefix-images-after.txt"
