#!/bin/bash
set -euo pipefail
if test "${SYSTRAP_SAMEPATH_DEADLINE:-}" != 1; then
  exec timeout --signal=TERM --kill-after=180s 300m env SYSTRAP_SAMEPATH_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 5a397632d896158997ee7594866b33d741c56917
test "$BASE" = bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa
test "$(git show -s --format=%P "$SOURCE")" = "$BASE"
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
git diff --quiet --exit-code
git diff --cached --quiet --exit-code
support=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/systrap-samepath"
mkdir -m 700 "$w"
results="$RESULT_DIR/samepath"
mkdir -m 700 "$results"
cleanup() {
  local rc=$?
  trap - EXIT TERM INT
  timeout --kill-after=10s 120s python3 "$support/docker_cleanup.py" "$w" "$results" || rc=1
  git switch --detach "$SOURCE" || rc=1
  git diff --quiet --exit-code || rc=1
  git diff --cached --quiet --exit-code || rc=1
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
run() {
  local label="$1" cap="$2" rc=0
  shift 2
  printf '%q ' "$@" >> "$results/commands.txt"
  printf '\n' >> "$results/commands.txt"
  timeout --signal=TERM --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$label.log" || rc=$?
  printf '%s\t%s\n' "$label" "$rc" >> "$results/status.tsv"
  return "$rc"
}
for tool in python3 readelf nm objdump tar timeout go docker; do command -v "$tool"; done
test "$(df -Pk "$RUNNER_TEMP" | awk 'NR==2 {print $4}')" -ge 12582912
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
cat /proc/cpuinfo > "$results/cpuinfo.txt"
go version > "$results/host-go-version.txt"
readelf --version > "$results/readelf-version.txt"
nm --version > "$results/nm-version.txt"
objdump --version > "$results/objdump-version.txt"
# Match prior successful native systrap qualification before any project build.
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-userns-before.txt"
  run apparmor-userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
  cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-userns-active.txt"
  test "$(cat "$results/apparmor-userns-active.txt")" = 0
else
  echo 'not-present' > "$results/apparmor-userns-active.txt"
fi
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' 'BAZEL_OPTIONS=--config=x86_64 --jobs=3 --nostamp --color=no --curses=no --remote_executor= --remote_cache= --bes_backend=')
reproducible=true
# Everything through the explicit freeze boundary is preparation. Build and
# retain the complete opaque releases, never hand-selected runtime members.
for arm in base candidate; do
  revision="$BASE"; cap=45m
  if test "$arm" = candidate; then revision="$SOURCE"; cap=25m; fi
  git switch --detach "$revision"
  mkdir -m 700 "$w/$arm" "$w/$arm/release" "$results/$arm"
  run "$arm-release" "$cap" make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$arm/release"
  run "$arm-archive" 3m tar -C "$w/$arm/release" -czf "$results/$arm/release.tar.gz" .
  run "$arm-ELF-layout" 5m python3 "$support/elf_inventory.py" "$w/$arm/release" "$results/$arm"
  run "$arm-reproduction" 1m python3 "$support/fileset.py" freeze "$w/$arm/release" "$support/expected-$arm-inventory.json" "$results/$arm/frozen-fileset.json" || reproducible=false
done
run compare-layouts 1m python3 "$support/compare.py" "$results"
if test "$reproducible" != true; then
  echo 'Prior complete ELF hashes did not reproduce. Measurements blocked; inspect the retained archives/section evidence before changing any requirement.' > "$results/measurement-blocked.txt"
  exit 1
fi
# Freeze one canonical BASE harness and all its runfiles, then never switch
# revisions or invoke a build tool during measured visits.
git switch --detach "$BASE"
mkdir -m 700 "$w/harness" "$w/tools" "$w/tools/bin"
run build-harness 15m make "${make_args[@]}" build 'TARGETS=//test/benchmarks/base:syscallbench_test //test/benchmarks/base:startup_test' OPTIONS=--enable_runfiles
timeout --kill-after=30s 2m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc cquery --config=x86_64 --nostamp --enable_runfiles --remote_executor= --remote_cache= --bes_backend= --output=files 'set(//test/benchmarks/base:syscallbench_test //test/benchmarks/base:startup_test)' > "$results/harness-paths.txt"
run freeze-harness 2m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json"
run harness-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
# Preserve the owning rule and Make recipes; no custom benchmark replaces them.
git show "$BASE:test/benchmarks/defs.bzl" > "$results/benchmark-owning-rule.bzl.txt"
git show "$BASE:Makefile" > "$results/benchmark-owning-Makefile.txt"
run syscallbench-image 5m make "${make_args[@]}" load-benchmarks_syscallbench
run alpine-image 5m make "${make_args[@]}" load-benchmarks_alpine
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/syscallbench:latest gvisor.dev/images/benchmarks/alpine:latest > "$results/image-identities.txt"
# This pinned external analysis tool uses the host Go's existing canonical
# dependency cache. It never builds/tests project sources and lives in task tmp.
run benchstat-install 8m bash -c 'cd "$1"; GOBIN="$1/bin" GOWORK=off go install golang.org/x/perf/cmd/benchstat@4febdb082b2ce2a0f9f6f496698aa5a2273279b8' bash "$w/tools"
go version -m "$w/tools/bin/benchstat" > "$results/benchstat-version.txt"
run bazel-shutdown 2m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown
# Refuse to interfere with preexisting Docker workloads. Configuration backup
# stays in private task scratch; artifacts retain only restoration hashes.
test -z "$(timeout 30s sudo docker ps -aq)"
test ! -L /etc/docker/daemon.json
if test -e /etc/docker/daemon.json; then
  sudo cp -a /etc/docker/daemon.json "$w/docker-original.json"
  echo present > "$w/docker-config-state"
else
  echo absent > "$w/docker-config-state"
fi
active="$w/active"
cp -a "$w/base/release" "$active"
mkdir -m 700 "$w/runtime-logs"
run register-runtime 1m sudo "$active/runsc" install --config_file=/etc/docker/daemon.json --experimental=true --runtime=systrap -- --debug-log "$w/runtime-logs/runsc.log.%TEST%.%TIMESTAMP%.%COMMAND%" --allow-suid --platform systrap --profile --TESTONLY-test-name-env=RUNSC_TEST_NAME
run reload-docker 1m sudo systemctl reload docker
sleep 3
timeout 30s sudo docker info --format '{{json .Runtimes.systrap}}' > "$results/registered-runtime.txt"
python3 - "$results" <<'PY'
import json, random, sys
from pathlib import Path
root = Path(sys.argv[1]); rng = random.Random(174463026)
labels = ['base', 'candidate', 'controlA', 'controlB']; rng.shuffle(labels)
rotations = list(range(4)); rng.shuffle(rotations)
orders = [labels[n:] + labels[:n] for n in rotations]
(root / 'schedule.json').write_text(json.dumps({'seed':174463026, 'orders':orders,
    'warmupsPerSuitePerVisit':3, 'measuredPerSuitePerVisit':6,
    'activePathIdenticalForEveryLabel':True}, indent=2)+'\n')
(root / 'schedule.tsv').write_text(''.join(f'{batch}\t{label}\n' for batch, order in enumerate(orders,1) for label in order))
PY
printf '%s\n' 'FROZEN: all builds, dependency installation and source switches finished; visits invoke only the frozen canonical benchmark executables.' > "$results/build-free-boundary.txt"
# Each visit has an outer eight-minute deadline including four benchmark
# invocations, hash checks and container cleanup. The complete measurement
# phase is capped at128m by the fixed16-visit schedule.
while IFS=$'\t' read -r batch label; do
  arm=base; if test "$label" = candidate; then arm=candidate; fi
  run "$label-$batch-visit" 8m bash "$support/visit.sh" "$w" "$results" "$support" "$arm" "$label" "$batch"
done < "$results/schedule.tsv"
run sample-accounting 2m python3 "$support/analyze.py" "$results"
for suite in syscallbench startup; do
  run "$suite-base-candidate-benchstat" 1m "$w/tools/bin/benchstat" "$results/base-$suite-clean.txt" "$results/candidate-$suite-clean.txt"
  run "$suite-controlAA-benchstat" 1m "$w/tools/bin/benchstat" "$results/controlA-$suite-clean.txt" "$results/controlB-$suite-clean.txt"
done
cat > "$results/prior-adverse-evidence.txt" <<'PRIOR'
Earlier results remain evidence, not superseded by this run:
44190929074 EPYC9V45 optimizedgetpid +3.22% p=.008 n24.
44196112563 EPYC7763 optimizedgetpid +1.20% p=.003 n24; ordinarygetpid +1.26% p=.032.
Identical-base A/A p=.907 ordinary and .354 optimized. Earlier candidate used
a different executable path from base/control; earlier complete runtime ELFs
were not retained. This run addresses the path confound with newly reproduced
matched --nostamp artifacts; it does not retroactively establish an earlier cause.
PRIOR
