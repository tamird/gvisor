#!/bin/bash
# Candidate-only default-argument capability; root scope/cost approval and independent executable review complete.
set -euo pipefail
if test "${DEFAULT_RACE_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 87m env DEFAULT_RACE_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = 7eafb7afe2c0255c98392c2e701ec6aecad22b27
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse "$SOURCE^")" = "$BASE"
case "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" in
 amd64:x86_64:x86_64) suite=syscall; other=syscallbench_test ;;
 arm64:aarch64:aarch64) suite=hackbench; other=hackbench_test ;;
 *) exit 2 ;;
esac
test -z "${COVERAGE_DIR:-}${GOCOVERDIR:-}${GO_TEST_RUN_FROM_BAZEL:-}"
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/coverage-default-race"
mkdir -m 700 "$w" "$w/runtime" "$w/harness"
results=$(realpath "$RESULT_DIR")
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 timeout --kill-after=10s 90s python3 "$support/docker_cleanup.py" "$w" "$results" || rc=1
 timeout --kill-after=5s 15s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 timeout --kill-after=5s 20s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local label="$1" cap="$2" start rc=0
 shift 2
 start=$(date +%s)
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$label.log" || rc=$?
 printf '%s\t%s\t%s\n' "$label" "$(( $(date +%s)-start ))" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
sudo -n true
command -v gcc
command -v g++
gcc --version > "$results/gcc-version.txt"
g++ --version > "$results/gxx-version.txt"
uname -a > "$results/worker.txt"
lscpu > "$results/cpu.txt"
git diff --quiet
git diff --cached --quiet
test -z "$(timeout 20s sudo docker ps -aq)"
sudo docker info --format '{{json .Runtimes}}' > "$results/runtimes-before.json"
python3 - "$results/runtimes-before.json" <<'NAMES'
import json
import sys
assert 'systrap' not in json.load(open(sys.argv[1]))
NAMES
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 stage apparmor-userns 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=("--config=$QUAL_CONFIG" --jobs=3 --strip=never --nostamp --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
git switch --detach "$BASE"
stage image-alpine 10m "${bazel_cmd[@]}" run "${flags[@]}" //images:benchmark_alpine_load
if test "$suite" = syscall; then
 stage image-syscallbench 10m "${bazel_cmd[@]}" run "${flags[@]}" //images/benchmarks/syscallbench:syscallbench_load
 image=syscallbench
else
 stage image-hackbench 10m make "${make_args[@]}" pull-benchmarks_hackbench
 image=hackbench
fi
for name in alpine "$image"; do
 sudo docker image inspect "gvisor.dev/images/benchmarks/$name:latest" > "$results/image-$name.json"
 test "$(sudo docker image inspect --format '{{.Architecture}}' "gvisor.dev/images/benchmarks/$name:latest")" = "$QUAL_ARCH"
done
targets=(//test/benchmarks/base:startup_test "//test/benchmarks/base:$other")
stage harness-build 15m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles "${targets[@]}"
timeout --kill-after=10s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files "set(${targets[*]})" > "$results/harness-paths.txt"
stage harness-freeze 2m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json" "$QUAL_ARCH" startup_test "$other"
stage harness-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
stage harness-archive 2m tar -C "$w/harness" -czf "$results/frozen-harness.tar.gz" .
git switch --detach "$SOURCE"
stage release-copy 25m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/runtime/"
for entry in '//runsc:runsc_race_coverage=runsc' '//runsc/cmd/sentry:gvisor_sentry_race_coverage=gvisor-bin/gvisor_sentry'; do
 label=${entry%%=*};slot=${entry#*=}
 stage "${slot##*/}-build" 20m "${bazel_cmd[@]}" build "${flags[@]}" "$label"
 path=$(timeout --kill-after=10s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" --output=files "$label")
 test "$(printf '%s\n' "$path" | wc -l)" -eq 1
 cp --remove-destination "$path" "$w/runtime/$slot"
 readelf -l "$w/runtime/$slot" > "$results/${slot##*/}-elf.txt"
 if grep -q INTERP "$results/${slot##*/}-elf.txt"; then exit 1; fi
 stage "${slot##*/}-version" 2m "$w/runtime/$slot" --version
done
stage symbolize 2m "$w/runtime/gvisor-bin/gvisor_sentry" symbolize -all
grep -q 'pkg/sentry/kernel/' "$results/symbolize.log"
if grep -Eq '/pkg/(coverage/|ring0/|sleep/|sync/|syncevent/|bpf/|sentry/platform/)' "$results/symbolize.log"; then exit 1; fi
stage runtime-freeze 1m python3 "$support/fileset.py" snapshot "$w/runtime" "$results/frozen-runtime.json"
stage runtime-archive 2m tar -C "$w/runtime" -czf "$results/runtime.tar.gz" .
sudo docker image inspect --format '{{.Id}}' "gvisor.dev/images/benchmarks/alpine:latest" "gvisor.dev/images/benchmarks/$image:latest" > "$results/frozen-images.txt"
stage bazel-shutdown 2m "${bazel_cmd[@]}" shutdown
test -z "$(timeout 20s sudo docker ps -aq)"
test ! -L /etc/docker/daemon.json
if sudo test -e /etc/docker/daemon.json; then
 sudo cp -a /etc/docker/daemon.json "$w/docker-original.json";echo present > "$w/docker-config-state"
else
 echo absent > "$w/docker-config-state"
fi
cp -a "$w/runtime" "$w/active"
stage register-runtime 1m sudo "$w/active/runsc" install --config_file=/etc/docker/daemon.json --runtime=systrap --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
stage restart-docker 1m sudo systemctl restart docker
stage runtime-ready 80s sudo python3 "$support/docker_runtime_ready.py" "$results/runtime-readiness.json" /etc/docker/daemon.json "systrap=$w/active/runsc"
sudo python3 - "$results/runtime-readiness.json" <<'DEFAULTARGS'
import json
from pathlib import Path
import sys
expected = ['--platform=systrap', '--network=sandbox', '--debug=false']
configuration = json.loads(Path('/etc/docker/daemon.json').read_text())['runtimes']['systrap']
observed = json.loads(Path(sys.argv[1]).read_text())['observations'][-1]['taskRuntimes']['systrap']
assert configuration['runtimeArgs'] == expected and observed['runtimeArgs'] == expected
DEFAULTARGS
python3 "$support/fileset.py" verify "$w/active" "$results/frozen-runtime.json" "$results/runtime-before.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/harness-before.json"
for selected in startup "$suite"; do
 binary=startup_test;selector=BenchmarkStartupEmpty;benchtime=1x;cap=2m
 if test "$selected" = syscall;then binary=$other;selector=Benchmark;benchtime=1s;cap=5m;fi
 if test "$selected" = hackbench;then binary=$other;selector=BenchmarkHackbench;benchtime=1x;cap=5m;fi
 runfiles="$w/harness/$binary.runfiles"
 mkdir -p "$w/bench-tmp" "$results/$selected-output"
 stage "$selected" "$cap" sudo env RUNTIME=systrap GOMAXPROCS=4 RUNFILES_DIR="$runfiles" TEST_SRCDIR="$runfiles" TEST_WORKSPACE=_main TMPDIR="$w/bench-tmp" TEST_TMPDIR="$w/bench-tmp" TEST_UNDECLARED_OUTPUTS_DIR="$results/$selected-output" "$w/harness/$binary" -runtime=systrap -test.run=NoTests "-test.bench=$selector" "-test.benchtime=$benchtime" -test.count=1 -test.cpu=4
 python3 "$support/validate.py" "$results/$selected.log" "$results/$selected.json" "$selected" "$QUAL_ARCH"
done
test -z "$(timeout 20s sudo docker ps -aq)"
timeout 30s sudo python3 "$support/quiescent.py" "$w/active"
python3 "$support/fileset.py" verify "$w/active" "$results/frozen-runtime.json" "$results/runtime-after.json"
python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/harness-after.json"
sudo docker image inspect --format '{{.Id}}' "gvisor.dev/images/benchmarks/alpine:latest" "gvisor.dev/images/benchmarks/$image:latest" > "$results/images-after.txt"
cmp "$results/frozen-images.txt" "$results/images-after.txt"
printf '%s\n' 'Candidate-only default debug=false/report-unset capability. No repaired BASE, comparative timing, n24, report or KCOV-consumption performance claim.' > "$results/scope.txt"
