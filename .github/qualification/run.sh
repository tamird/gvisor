#!/bin/bash
# Capability pilot only; no statistical performance claim.
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = f9446ddf973def4ce653acbb098532c2b1eb0ef0
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_CONFIG" = x86_64
w="$RUNNER_TEMP/tcp-loss-pilot"
mkdir -m 700 "$w"
cleanup() { git switch --detach "$SOURCE"; }
trap cleanup EXIT
flags='--config=x86_64 -c opt --jobs=3 --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
uname -a > "$RESULT_DIR/worker.txt"
sudo -n true
missing=false
for binary in iperf3 tc ip bc lsmod modprobe timeout; do
 if ! command -v "$binary"; then missing=true; fi
done
if test "$missing" = true; then
 sudo apt-get update
 sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iperf3 iproute2 bc kmod coreutils
fi
iperf3 --version > "$RESULT_DIR/iperf3-version.txt"
tc -V > "$RESULT_DIR/tc-version.txt"
dpkg-query -W iperf3 iproute2 bc kmod coreutils > "$RESULT_DIR/packages.tsv"
# A built-in netem implementation may not have a loadable module. The actual
# namespace/qdisc probe below decides capability even if modprobe fails.
sudo modprobe sch_netem 2> "$RESULT_DIR/modprobe-netem.log" || true
sudo unshare --net bash -eu -c 'ip link add pilot0 type veth peer name pilot1; ip link set pilot0 up; tc qdisc add dev pilot0 root netem delay 5ms loss random 0.5%; tc -s qdisc show dev pilot0' > "$RESULT_DIR/netem-capability.txt"
# Freeze only standalone benchmark helpers. These are not a runsc release.
for side in base candidate; do
 if test "$side" = base; then sha="$BASE"; else sha="$SOURCE"; fi
 git switch --detach "$sha"
 test "$(git rev-parse HEAD)" = "$sha"
 mkdir "$w/$side"
 start=$(date +%s)
 for helper in tcp_proxy nsjoin; do
  make "${make_args[@]}" copy "TARGETS=//test/benchmarks/tcp:$helper" "DESTINATION=$w/$side/$helper" 2>&1 | tee "$RESULT_DIR/build-$side-$helper.log"
 done
 git show "$sha:test/benchmarks/tcp/tcp_benchmark.sh" > "$w/$side/tcp_benchmark.sh"
 chmod 755 "$w/$side/tcp_proxy" "$w/$side/nsjoin"
 sha256sum "$w/$side/tcp_proxy" "$w/$side/nsjoin" "$w/$side/tcp_benchmark.sh" > "$RESULT_DIR/helpers-$side.sha256"
 git ls-tree "$sha" test/benchmarks/tcp/tcp_benchmark.sh test/benchmarks/tcp/tcp_proxy.go test/benchmarks/tcp/nsjoin.c test/benchmarks/tcp/xdp.go > "$RESULT_DIR/source-inputs-$side.txt"
 printf '%s\t%s\t%s\n' "$side" "$sha" "$(( $(date +%s)-start ))" >> "$RESULT_DIR/build-seconds.tsv"
done
validate() {
 python3 - "$1" <<'PY'
import math
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text()
rows = re.findall(r'^(BenchmarkTCP/\S+)\s+1\s+([0-9.eE+-]+) Mb/s\s+([0-9.eE+-]+) cpu-time$', text, re.MULTILINE)
assert len(rows) == 1, (path, rows)
name, throughput, cpu = rows[0]
assert math.isfinite(float(throughput)) and float(throughput) > 0, (path, throughput)
assert math.isfinite(float(cpu)) and float(cpu) >= 0, (path, cpu)
assert '/role=client/' in name, (path, name)
PY
}
# Source defaults client=false/server=false: --linux-client changes only the
# reported role. Both proxies therefore use Linux networking for this baseline.
git switch --detach "$BASE"
start=$(date +%s)
timeout --kill-after=15s 180s make "${make_args[@]}" sudo TARGETS=//test/benchmarks/tcp:tcp_benchmark "ARGS=--linux-client --ideal --latency 10 --loss 1 --duration 30 --helpers $w/base" > "$RESULT_DIR/native-reference.log" 2>&1
validate "$RESULT_DIR/native-reference.log"
printf 'native\t%s\n' "$(( $(date +%s)-start ))" > "$RESULT_DIR/native-seconds.tsv"
# Equal warmups; measured ABBA order, only two pilot samples per source.
for round in warmup sample1 sample2; do
 if test "$round" = sample2; then order='candidate base'; else order='base candidate'; fi
 for side in $order; do
  if test "$side" = base; then sha="$BASE"; else sha="$SOURCE"; fi
  git switch --detach "$sha"
  sha256sum --check "$RESULT_DIR/helpers-$side.sha256"
  start=$(date +%s)
  timeout --kill-after=15s 180s make "${make_args[@]}" sudo TARGETS=//test/benchmarks/tcp:tcp_benchmark "ARGS=--client --ideal --latency 10 --loss 1 --duration 30 --helpers $w/$side" > "$RESULT_DIR/$round-$side.log" 2>&1
  validate "$RESULT_DIR/$round-$side.log"
  sha256sum --check "$RESULT_DIR/helpers-$side.sha256"
  printf '%s\t%s\t%s\t%s\n' "$round" "$side" "$sha" "$(( $(date +%s)-start ))" >> "$RESULT_DIR/pilot-seconds.tsv"
 done
done
# Both sets of compiled helpers remain immutable and source-bound throughout.
