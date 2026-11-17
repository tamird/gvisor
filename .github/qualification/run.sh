#!/bin/bash
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}"
test "$SOURCE" = 5a397632d896158997ee7594866b33d741c56917
test "$BASE" = bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git show -s --format=%P HEAD)" = "$BASE"
test "$(bazel --version)" = 'bazel 9.3.1'
w="$RUNNER_TEMP/atomic-controlled-replication"
mkdir -m 700 "$w" "$w/bin"
cp "$(dirname "${BASH_SOURCE[0]}")/analyze.py" "$w/analyze.py"
uname -a > "$RESULT_DIR/worker.txt"
lscpu > "$RESULT_DIR/lscpu.txt"
cat /proc/cpuinfo > "$RESULT_DIR/cpuinfo.txt"
bazel --version > "$RESULT_DIR/bazel-version.txt"
sudo apt-get update
sudo apt-get install -y build-essential python3 unzip zip xz-utils
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
# Go is used only for pinned external benchmark analysis, never gVisor builds.
command -v go
go version > "$RESULT_DIR/host-go-version.txt"
make_args=(DOCKER_BUILD=false "BAZEL=bazel --host_jvm_args=-Xmx4g" "BAZEL_OPTIONS=--config=x86_64 --jobs=3")
run() {
  local label="$1"; shift
  local rc=0
  "$@" 2>&1 | tee "$RESULT_DIR/$label.log" || rc=$?
  printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$label" "$rc" >> "$RESULT_DIR/commands.tsv"
  return "$rc"
}
# Build each runtime once through the canonical Make/Bazel rule. Freeze its
# bytes for all batches, including both independent labels of the A/A control.
for arm in base candidate; do
  revision="$BASE"; test "$arm" != candidate || revision="$SOURCE"
  git switch --detach "$revision"
  run "$arm-runtime" make "${make_args[@]}" "$w/$arm/runsc" "RUNTIME_DIR=$w/$arm"
  sha256sum "$w/$arm/runsc" > "$RESULT_DIR/$arm-runsc.sha256"
done
run syscallbench-image make "${make_args[@]}" load-benchmarks_syscallbench
python3 - "$RESULT_DIR" <<'PY'
import json, random, sys
from pathlib import Path
seed = 174462026
rng = random.Random(seed)
orders = []
# Four cyclic rotations give each label each temporal position exactly once;
# seeded randomization chooses the cycle and rotation order in advance.
labels = ['base', 'candidate', 'controlA', 'controlB']
rng.shuffle(labels)
rotations = list(range(4))
rng.shuffle(rotations)
for rotation in rotations:
    orders.append(labels[rotation:] + labels[:rotation])
p = Path(sys.argv[1])
(p / 'schedule.json').write_text(json.dumps({'seed': seed, 'orders': orders, 'warmupCountPerVisit': 3, 'measuredCountPerVisit': 6, 'note': 'Original run already used AB,BA,AB,BA; this independent run adds balanced seeded four-label placement and exact-base A/A control.'}, indent=2)+'\n')
(p / 'schedule.tsv').write_text(''.join(f'{i}\t{label}\n' for i, order in enumerate(orders, 1) for label in order))
PY
while IFS=$'\t' read -r batch label; do
  arm=base; test "$label" != candidate || arm=candidate
  revision="$BASE"; test "$arm" != candidate || revision="$SOURCE"
  git diff --exit-code
  git switch --detach "$revision"
  sha256sum -c "$RESULT_DIR/$arm-runsc.sha256"
  runtime="$w/$arm/runsc"
  # -o skips only already-loaded image and immutable prebuilt runtime targets.
  # Harness binaries still use the canonical Bazel-backed Make execution path.
  args=(-o load-benchmarks -o "$runtime" "${make_args[@]}" benchmark-platforms BENCHMARKS_TARGETS=//test/benchmarks/base:syscallbench_test BENCHMARKS_PLATFORMS=systrap BENCHMARKS_RUNC=false 'BENCHMARKS_FILTER=BenchmarkSyscallbench' "RUNTIME_DIR=$w/$arm")
  date -u +%FT%TZ > "$RESULT_DIR/$label-batch$batch-start.txt"
  run "$label-warmup-$batch" make "${args[@]}" 'BENCHMARKS_OPTIONS=-test.benchtime=1s -test.count=3 -test.cpu=4'
  run "$label-measured-$batch" make "${args[@]}" 'BENCHMARKS_OPTIONS=-test.benchtime=1s -test.count=6 -test.cpu=4'
  sha256sum -c "$RESULT_DIR/$arm-runsc.sha256"
  date -u +%FT%TZ > "$RESULT_DIR/$label-batch$batch-end.txt"
done < "$RESULT_DIR/schedule.tsv"
python3 "$w/analyze.py" "$RESULT_DIR"
(cd "$w" && GOBIN="$w/bin" GOWORK=off go install golang.org/x/perf/cmd/benchstat@4febdb082b2ce2a0f9f6f496698aa5a2273279b8)
go version -m "$w/bin/benchstat" > "$RESULT_DIR/benchstat-version.txt"
"$w/bin/benchstat" "$RESULT_DIR/base-clean.txt" "$RESULT_DIR/candidate-clean.txt" | tee "$RESULT_DIR/base-candidate-benchstat.txt"
"$w/bin/benchstat" "$RESULT_DIR/controlA-clean.txt" "$RESULT_DIR/controlB-clean.txt" | tee "$RESULT_DIR/control-AA-benchstat.txt"
git switch --detach "$SOURCE"
# The dispatcher independently checks final HEAD, tracked worktree and index.
