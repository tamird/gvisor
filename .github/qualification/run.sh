#!/bin/bash
# Draft support-only remote plan. Run one category per source-bound hosted job.
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${QUAL_CONFIG:?}"
CATEGORY=nginx
test "$SOURCE" = f9446ddf973def4ce653acbb098532c2b1eb0ef0
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_CONFIG" = x86_64
w="$RUNNER_TEMP/tcp-nginx-partition-pilot"
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
target=//test/benchmarks/network:nginx_test
images=(nginx hey)
# ParametersToName uses name.value, preserving operation then parallel order.
cat > "$RESULT_DIR/expected-leaves.txt" <<'LEAVES'
BenchmarkNginxDocSize/filesize.notfound/concurrency.1/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.notfound/concurrency.1/filesystem.bind
BenchmarkNginxDocSize/filesize.notfound/concurrency.25/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.notfound/concurrency.25/filesystem.bind
BenchmarkNginxDocSize/filesize.notfound/concurrency.50/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.notfound/concurrency.50/filesystem.bind
BenchmarkNginxDocSize/filesize.notfound/concurrency.100/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.notfound/concurrency.100/filesystem.bind
BenchmarkNginxDocSize/filesize.notfound/concurrency.1000/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.notfound/concurrency.1000/filesystem.bind
BenchmarkNginxDocSize/filesize.1Kb/concurrency.1/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Kb/concurrency.1/filesystem.bind
BenchmarkNginxDocSize/filesize.1Kb/concurrency.25/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Kb/concurrency.25/filesystem.bind
BenchmarkNginxDocSize/filesize.1Kb/concurrency.50/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Kb/concurrency.50/filesystem.bind
BenchmarkNginxDocSize/filesize.1Kb/concurrency.100/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Kb/concurrency.100/filesystem.bind
BenchmarkNginxDocSize/filesize.1Kb/concurrency.1000/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Kb/concurrency.1000/filesystem.bind
BenchmarkNginxDocSize/filesize.10Kb/concurrency.1/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Kb/concurrency.1/filesystem.bind
BenchmarkNginxDocSize/filesize.10Kb/concurrency.25/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Kb/concurrency.25/filesystem.bind
BenchmarkNginxDocSize/filesize.10Kb/concurrency.50/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Kb/concurrency.50/filesystem.bind
BenchmarkNginxDocSize/filesize.10Kb/concurrency.100/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Kb/concurrency.100/filesystem.bind
BenchmarkNginxDocSize/filesize.10Kb/concurrency.1000/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Kb/concurrency.1000/filesystem.bind
BenchmarkNginxDocSize/filesize.100Kb/concurrency.1/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.100Kb/concurrency.1/filesystem.bind
BenchmarkNginxDocSize/filesize.100Kb/concurrency.25/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.100Kb/concurrency.25/filesystem.bind
BenchmarkNginxDocSize/filesize.100Kb/concurrency.50/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.100Kb/concurrency.50/filesystem.bind
BenchmarkNginxDocSize/filesize.100Kb/concurrency.100/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.100Kb/concurrency.100/filesystem.bind
BenchmarkNginxDocSize/filesize.100Kb/concurrency.1000/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.100Kb/concurrency.1000/filesystem.bind
BenchmarkNginxDocSize/filesize.1Mb/concurrency.1/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Mb/concurrency.1/filesystem.bind
BenchmarkNginxDocSize/filesize.1Mb/concurrency.25/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Mb/concurrency.25/filesystem.bind
BenchmarkNginxDocSize/filesize.1Mb/concurrency.50/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Mb/concurrency.50/filesystem.bind
BenchmarkNginxDocSize/filesize.1Mb/concurrency.100/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Mb/concurrency.100/filesystem.bind
BenchmarkNginxDocSize/filesize.1Mb/concurrency.1000/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.1Mb/concurrency.1000/filesystem.bind
BenchmarkNginxDocSize/filesize.10Mb/concurrency.1/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Mb/concurrency.1/filesystem.bind
BenchmarkNginxDocSize/filesize.10Mb/concurrency.25/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Mb/concurrency.25/filesystem.bind
BenchmarkNginxDocSize/filesize.10Mb/concurrency.50/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Mb/concurrency.50/filesystem.bind
BenchmarkNginxDocSize/filesize.10Mb/concurrency.100/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Mb/concurrency.100/filesystem.bind
BenchmarkNginxDocSize/filesize.10Mb/concurrency.1000/filesystem.tmpfs
BenchmarkNginxDocSize/filesize.10Mb/concurrency.1000/filesystem.bind
BenchmarkContinuousNginx/filesize.10Kb/concurrency.1/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.10Kb/concurrency.25/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.10Kb/concurrency.100/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.10Kb/concurrency.1000/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.100Kb/concurrency.1/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.100Kb/concurrency.25/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.100Kb/concurrency.100/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.100Kb/concurrency.1000/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.1Mb/concurrency.1/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.1Mb/concurrency.25/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.1Mb/concurrency.100/filesystem.tmpfs
BenchmarkContinuousNginx/filesize.1Mb/concurrency.1000/filesystem.tmpfs
LEAVES
# Pull-only canonical input cache: a missing image stops this pilot. No Docker builds.
for image in "${images[@]}"; do
 timeout --kill-after=30s 10m make "${make_args[@]}" "pull-benchmarks_$image"
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
validate_group() {
 python3 - "$1" "$2" "$3" "$RESULT_DIR/expected-leaves.txt" <<'PYGROUP'
from pathlib import Path
import math
import re
import sys

path = Path(sys.argv[1])
concurrency = int(sys.argv[2])
iterations = int(sys.argv[3])
expected = {name for name in Path(sys.argv[4]).read_text().splitlines() if name.split('/')[2] == f'concurrency.{concurrency}'}
text = path.read_text()
assert not re.search(r'(^|\n)(FAIL|--- SKIP:)', text), path
rows = re.findall(r'^(Benchmark\S+)\s+(\d+)\s+([0-9.eE+-]+)\s+ns/op', text, re.MULTILINE)
names = [re.sub(r'-\d+$', '', name) for name, _, _ in rows]
assert len(names) == len(expected) and set(names) == expected, (path, names, sorted(expected))
assert all(int(n) == iterations and math.isfinite(float(ns)) and float(ns) > 0 for _, n, ns in rows), (path, rows)
path.with_suffix('.iterations.tsv').write_text(''.join(f'{name}\t{n}\n' for name, n, _ in rows))
PYGROUP
}
# Smallest real Go matcher group gates both sources before costlier groups.
# Each connection receives one request. Capability only, no throughput claim.
for concurrency in 1 25 50 100 1000; do
 for side in base candidate; do
  if test "$side" = base; then sha="$BASE"; else sha="$SOURCE"; fi
  git switch --detach "$sha"
  sha256sum --check "$RESULT_DIR/release-$side.sha256"
  python3 "$manifest_tool" verify "$w/$side" "$RESULT_DIR/release-$side.manifest.json"
  # Four literal backslashes survive tools/bazel.mk's outer and xargs bash
  # quoting as one Go-regexp \z anchor. No Make dollar-anchor expansion.
  selector="Benchmark/filesize/concurrency[.]$concurrency"'\\\\z/filesystem'
  args="-runtime=tcp-pilot-$side -test.run=NoTests '-test.bench=$selector' -test.benchtime=${concurrency}x -test.count=1 -test.timeout=7m"
  printf '%s\t%s\t%s\n' "$side" "$concurrency" "$args" >> "$RESULT_DIR/selectors.tsv"
  start=$(date +%s)
  timeout --kill-after=15s 8m make "${make_args[@]}" sudo "TARGETS=$target" "ARGS=$args" > "$RESULT_DIR/capability-$side-$concurrency.log" 2>&1
  validate_group "$RESULT_DIR/capability-$side-$concurrency.log" "$concurrency" "$concurrency"
  sha256sum --check "$RESULT_DIR/release-$side.sha256"
  python3 "$manifest_tool" verify "$w/$side" "$RESULT_DIR/release-$side.manifest.json"
  printf '%s\t%s\t%s\t%s\n' "$concurrency" "$side" "$sha" "$(( $(date +%s)-start ))" >> "$RESULT_DIR/pilot-seconds.tsv"
 done
done
python3 - "$RESULT_DIR" <<'PYUNION'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
expected = set((root / 'expected-leaves.txt').read_text().splitlines())
for side in ('base', 'candidate'):
    paths = sorted(root.glob(f'capability-{side}-*.iterations.tsv'))
    assert len(paths) == 5, (side, paths)
    names = [re.sub(r'-\d+$', '', line.split('\t')[0]) for path in paths for line in path.read_text().splitlines()]
    assert len(names) == len(expected) == 72 and set(names) == expected, (side, names, expected)
(root / 'capability-complete.txt').write_text('All72variants/source once at each named concurrency; no warmup or statistical/throughput adequacy claim.\n')
PYUNION
