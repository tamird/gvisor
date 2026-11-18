#!/bin/bash
# Draft support-only remote plan. Run one category per source-bound hosted job.
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${QUAL_CONFIG:?}"
CATEGORY=iperf
test "$SOURCE" = f9446ddf973def4ce653acbb098532c2b1eb0ef0
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_CONFIG" = x86_64
w="$RUNNER_TEMP/tcp-iperf-gib-pilot"
manifest_tool="$(dirname "$0")/release_manifest.py"
test -f "$manifest_tool"
mkdir -m 700 "$w"
cleanup() { git switch --detach "$SOURCE"; }
trap cleanup EXIT
flags='--config=x86_64 -c opt --jobs=3 --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
uname -a > "$RESULT_DIR/worker.txt"
docker info > "$RESULT_DIR/docker-info.txt"
command -v timeout
sudo -n true
target=//test/benchmarks/network:iperf_test
images=(iperf)
# ParametersToName uses name.value, preserving operation then parallel order.
cat > "$RESULT_DIR/expected-leaves.txt" <<'LEAVES'
BenchmarkIperfOneConnection/operation.Upload
BenchmarkIperfOneConnection/operation.Download
BenchmarkIperfManyConnections/operation.Upload/parallel.4
BenchmarkIperfManyConnections/operation.Download/parallel.4
BenchmarkIperfManyConnections/operation.Upload/parallel.16
BenchmarkIperfManyConnections/operation.Download/parallel.16
BenchmarkIperfManyConnections/operation.Upload/parallel.64
BenchmarkIperfManyConnections/operation.Download/parallel.64
LEAVES
# Pull-only canonical input cache: a missing image stops this pilot. No Docker builds.
for image in "${images[@]}"; do
 make "${make_args[@]}" "pull-benchmarks_$image"
 docker image inspect "gvisor.dev/images/benchmarks/$image" >> "$RESULT_DIR/images.json"
done
# Freeze complete release filesets, preserving every member; never enumerate sidecars.
for side in base candidate; do
 if test "$side" = base; then sha="$BASE"; else sha="$SOURCE"; fi
 git switch --detach "$sha"
 mkdir "$w/$side"
 start=$(date +%s)
 timeout --kill-after=30s 15m make "${make_args[@]}" release-tarball "DESTINATION=$w/$side" 2>&1 | tee "$RESULT_DIR/build-$side.log"
 sha256sum "$w/$side/gvisor.tar.bz2" > "$RESULT_DIR/release-$side.sha256"
 tar -xf "$w/$side/gvisor.tar.bz2" -C "$w/$side"
 python3 "$manifest_tool" record "$w/$side" "$RESULT_DIR/release-$side.manifest.json"
 timeout --kill-after=30s 5m make "${make_args[@]}" build "TARGETS=$target"
 printf '%s\t%s\t%s\n' "$side" "$sha" "$(( $(date +%s)-start ))" >> "$RESULT_DIR/build-seconds.tsv"
 sudo "$w/$side/runsc" install --config_file=/etc/docker/daemon.json --runtime="tcp-pilot-$side" -- --platform=systrap --network=sandbox
done
sudo systemctl restart docker
# Equal warmup then ABBA pilot, without retaining warmups as statistical samples.
# Entire category runs, including every default variant. No regex metacharacters.
for round in warmup sample1 sample2; do
 if test "$round" = sample2; then order='candidate base'; else order='base candidate'; fi
 for side in $order; do
  if test "$side" = base; then sha="$BASE"; else sha="$SOURCE"; fi
  git switch --detach "$sha"
  sha256sum --check "$RESULT_DIR/release-$side.sha256"
  python3 "$manifest_tool" verify "$w/$side" "$RESULT_DIR/release-$side.manifest.json"
  start=$(date +%s)
  timeout --kill-after=15s 180s make "${make_args[@]}" sudo "TARGETS=$target" "ARGS=-runtime=tcp-pilot-$side -test.run=NoTests -test.bench=Benchmark -test.benchtime=1048576x -test.count=1 -test.timeout=150s" > "$RESULT_DIR/$round-$side.log" 2>&1
  sha256sum --check "$RESULT_DIR/release-$side.sha256"
  python3 "$manifest_tool" verify "$w/$side" "$RESULT_DIR/release-$side.manifest.json"
  printf '%s\t%s\t%s\t%s\n' "$round" "$side" "$sha" "$(( $(date +%s)-start ))" >> "$RESULT_DIR/pilot-seconds.tsv"
 done
done
python3 - "$RESULT_DIR" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
expected = set((root / 'expected-leaves.txt').read_text().splitlines())
paths = sorted(root.glob('sample*-*.log'))
assert len(paths) == 4, ("sample logs", paths)
for path in paths:
    text = path.read_text()
    assert not re.search(r'(^|\n)(FAIL|--- SKIP:)', text), ("failed/skipped", path)
    rows = re.findall(r'^(Benchmark\S+)\s+(\d+)\s+(.+)$', text, re.MULTILINE)
    names = [re.sub(r'-\d+$', '', name) for name, _, _ in rows]
    assert len(names) == len(expected), (path, "row count", names, expected)
    assert set(names) == expected, (path, "leaf names", names, expected)
    assert all(int(n) == 1048576 and not re.search(r'\b(?:NaN|[+-]?Inf)\b', metrics) for _, n, metrics in rows), (path, rows)
    path.with_suffix('.iterations.tsv').write_text(''.join(f'{name}\t{n}\n' for name, n, _ in rows))
PY
# Fixed volume: 1048576 KiB aggregate, minimum 16384 KiB per connection at parallel64.
# Pilot evidence only. Use measured wall time + actual final N to choose sampling
# benchtime and job partitioning. This is not a 24-sample performance comparison.
