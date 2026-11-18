#!/bin/bash
# Root-approved remaining-scope capability only; not a statistical study.
set -euo pipefail
if test "${COVERAGE_MODES_DEADLINE:-}" != 1; then
 case "${QUAL_ARCH:-}" in amd64) cap=120m ;; arm64) cap=150m ;; *) exit 2 ;; esac
 exec timeout --signal=TERM --kill-after=180s "$cap" env COVERAGE_MODES_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = 7eafb7afe2c0255c98392c2e701ec6aecad22b27
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse "$SOURCE^")" = "$BASE"
case "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" in
 amd64:x86_64:x86_64) modes=(race); suites=(startup syscall); other_cc=aarch64-linux-gnu ;;
 arm64:aarch64:aarch64) modes=(normal race); suites=(startup hackbench); other_cc=x86_64-linux-gnu ;;
 *) exit 2 ;;
esac
support="$(cd "$(dirname "$0")" && pwd)"
w="$RUNNER_TEMP/coverage-modes-pilot"
mkdir -m 700 "$w" "$w/inputs" "$w/inputs/bin" "$w/inputs/archives" "$w/harness"
mkdir -p "$RESULT_DIR"
results="$(realpath "$RESULT_DIR")"
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
 local name="$1" cap="$2" start rc=0
 shift 2
 start=$(date +%s)
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$name.log" || rc=$?
 printf '%s\t%s\t%s\n' "$name" "$(( $(date +%s)-start ))" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
command -v bazel
command -v timeout
sudo -n true
git diff --quiet
git diff --cached --quiet
uname -a > "$results/worker.txt"
lscpu > "$results/cpu.txt"
test -z "$(timeout 20s docker ps -aq)"
docker info --format '{{json .Runtimes}}' > "$results/runtimes-before.json"
python3 - "$results/runtimes-before.json" <<'BEFORE'
import json
import sys
runtimes = json.load(open(sys.argv[1]))
assert not {'coverage-pilot-base', 'coverage-pilot-candidate'} & runtimes.keys(), runtimes
BEFORE
if ! test -x "/usr/bin/$other_cc-gcc" || ! test -x "/usr/bin/$other_cc-g++"; then
 stage cross-toolchain-update 5m sudo apt-get update
 stage cross-toolchain-install 30m sudo env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=120 -o Acquire::https::Timeout=120 install -y --no-install-recommends "gcc-$other_cc" "g++-$other_cc"
fi
"/usr/bin/$other_cc-gcc" --version > "$results/cross-gcc-version.txt"
"/usr/bin/$other_cc-g++" --version > "$results/cross-gxx-version.txt"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 stage apparmor-userns 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
cp "$support/curl" "$w/inputs/bin/curl"
chmod 700 "$w/inputs/bin/curl"
cp "$support/go-pins.json" "$w/inputs/go-pins.json"
python3 - "$w/inputs/go-pins.json" "$QUAL_ARCH" > "$w/downloads.tsv" <<'PINS'
import json
import sys
files = json.load(open(sys.argv[1]))['files']
for name in ('go1.27.3.src.tar.gz', 'go1.27.3.linux-' + sys.argv[2] + '.tar.gz'):
    item, = [item for item in files if item['filename'] == name]
    print(name, item['sha256'], sep='\t')
PINS
while IFS=$'\t' read -r name digest; do
 stage "download-$name" 4m /usr/bin/curl --proto '=https' --proto-redir '=https' -fsSL --retry 3 --max-time 180 "https://dl.google.com/go/$name" -o "$w/inputs/archives/$name"
 printf '%s  %s\n' "$digest" "$w/inputs/archives/$name" | sha256sum -c -
done < "$w/downloads.tsv"
cp "$w/downloads.tsv" "$results/verified-go-inputs.tsv"
input_hash=$(cat "$w/inputs/bin/curl" "$w/inputs/go-pins.json" | sha256sum | cut -d' ' -f1)
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=("--config=$QUAL_CONFIG" --jobs=3 --strip=never --nostamp --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
verify_flags=("--action_env=PATH=$w/inputs/bin:$PATH" "--action_env=VERIFY_COVERAGE_ARCHIVES=$w/inputs/archives" "--action_env=VERIFY_COVERAGE_DOWNLOAD_AUDIT=$results/baseline-download-audit.tsv" "--action_env=VERIFY_COVERAGE_INPUT_SHA256=$input_hash" "--sandbox_add_mount_pair=$w/inputs" "--sandbox_writable_path=$results")
git switch --detach "$BASE"
stage alpine-image 10m "${bazel_cmd[@]}" run "${flags[@]}" //images:benchmark_alpine_load
targets=(//test/benchmarks/base:startup_test)
names=(startup_test)
if test "$QUAL_ARCH" = amd64; then
 stage syscallbench-image 10m "${bazel_cmd[@]}" run "${flags[@]}" //images/benchmarks/syscallbench:syscallbench_load
 targets+=(//test/benchmarks/base:syscallbench_test); names+=(syscallbench_test)
else
 stage hackbench-image 10m make "${make_args[@]}" pull-benchmarks_hackbench
 targets+=(//test/benchmarks/base:hackbench_test); names+=(hackbench_test)
fi
for image in alpine "${suites[1]/syscall/syscallbench}"; do
 docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"
 test "$(docker image inspect --format '{{.Architecture}}' "gvisor.dev/images/benchmarks/$image:latest")" = "$QUAL_ARCH"
done
stage harness-build 15m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles "${targets[@]}"
expression="set(${targets[*]})"
timeout --kill-after=30s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files "$expression" > "$results/harness-paths.txt"
stage harness-freeze 2m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json" "$QUAL_ARCH" "${names[@]}"
run_suite() {
 local prefix="$1" runtime="$2" suite="$3" binary selector benchtime cap
 binary=startup_test; selector=BenchmarkStartupEmpty; benchtime=1x; cap=2m
 if test "$suite" = syscall; then binary=syscallbench_test; selector=Benchmark; benchtime=1s; cap=5m; fi
 if test "$suite" = hackbench; then binary=hackbench_test; selector=BenchmarkHackbench; benchtime=1x; cap=5m; fi
 local runfiles="$w/harness/$binary.runfiles"
 mkdir -p "$w/bench-tmp" "$results/$prefix-output"
 stage "$prefix-$suite" "$cap" sudo env RUNTIME="$runtime" GOMAXPROCS=4 RUNFILES_DIR="$runfiles" TEST_SRCDIR="$runfiles" TEST_WORKSPACE=_main TMPDIR="$w/bench-tmp" TEST_TMPDIR="$w/bench-tmp" TEST_UNDECLARED_OUTPUTS_DIR="$results/$prefix-output" "$w/harness/$binary" "-runtime=$runtime" -test.run=NoTests "-test.bench=$selector" "-test.benchtime=$benchtime" -test.count=1 -test.cpu=4
 python3 "$support/validate.py" "$results/$prefix-$suite.log" "$results/$prefix-$suite.json" "$suite" "$QUAL_ARCH"
}
if test "$QUAL_ARCH" = arm64; then
 touch "$w/native-started"
 for suite in "${suites[@]}"; do run_suite native runc "$suite"; done
fi
base_actions=0
for side in base candidate; do
 revision="$BASE"; if test "$side" = candidate; then revision="$SOURCE"; fi
 git switch --detach "$revision"
 mkdir "$w/$side-plain"
 stage "$side-release-copy" 20m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$side-plain/"
 for mode in "${modes[@]}"; do
  mkdir "$w/$side-$mode"
  cp -a "$w/$side-plain" "$w/$side-$mode/runtime"
  runsc=runsc_coverage; sentry=gvisor_sentry_coverage
  if test "$mode" = race; then runsc=runsc_race_coverage; sentry=gvisor_sentry_race_coverage; fi
  for entry in "//runsc:$runsc=runsc" "//runsc/cmd/sentry:$sentry=gvisor-bin/gvisor_sentry"; do
   label="${entry%%=*}"; slot="${entry#*=}"
   extra=(); if test "$side" = base; then extra=("${verify_flags[@]}"); fi
   stage "$side-$mode-${label##*:}-build" 25m "${bazel_cmd[@]}" build "${flags[@]}" "${extra[@]}" "$label"
   path=$(timeout --kill-after=30s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" "${extra[@]}" --output=files "$label")
   test "$(printf '%s\n' "$path" | wc -l)" -eq 1
   cp --remove-destination "$path" "$w/$side-$mode/runtime/$slot"
   readelf -l "$w/$side-$mode/runtime/$slot" > "$results/$side-$mode-${slot##*/}-elf.txt"
   if grep -q INTERP "$results/$side-$mode-${slot##*/}-elf.txt" && { test "$mode" = normal || test "$side" = candidate; }; then
    echo 'Unexpected dynamic interpreter in normal or candidate static binary' >&2; exit 1
   fi
   stage "$side-$mode-${slot##*/}-version" 2m "$w/$side-$mode/runtime/$slot" --version
   if test "$side" = base; then
    base_actions=$((base_actions + 1))
    python3 - "$results/baseline-download-audit.tsv" "$w/downloads.tsv" "$base_actions" <<'AUDIT'
from pathlib import Path
import sys
rows = Path(sys.argv[1]).read_text().splitlines()
expected = {f'https://dl.google.com/go/{name}\t{digest}' for name, digest in
            (line.split('\t') for line in Path(sys.argv[2]).read_text().splitlines())}
count = int(sys.argv[3])
assert len(rows) == 2 * count and all(rows.count(item) == count for item in expected), (rows, expected, count)
AUDIT
   fi
  done
  stage "$side-$mode-runtime-freeze" 1m python3 "$support/fileset.py" snapshot "$w/$side-$mode/runtime" "$results/$side-$mode-runtime-manifest.json"
  stage "$side-$mode-runtime-archive" 2m tar -C "$w/$side-$mode/runtime" -czf "$results/$side-$mode-runtime.tar.gz" .
  stage "$side-$mode-symbolize" 2m "$w/$side-$mode/runtime/gvisor-bin/gvisor_sentry" symbolize -all
  grep -q 'pkg/sentry/kernel/' "$results/$side-$mode-symbolize.log"
  if grep -Eq '/pkg/(coverage/|ring0/|sleep/|sync/|syncevent/|bpf/|sentry/platform/)' "$results/$side-$mode-symbolize.log"; then exit 1; fi
 done
done
stage bazel-shutdown 2m "${bazel_cmd[@]}" shutdown
test -z "$(timeout 20s docker ps -aq)"
test ! -L /etc/docker/daemon.json
if sudo test -e /etc/docker/daemon.json; then
 sudo cp --preserve=mode,ownership,timestamps /etc/docker/daemon.json "$w/docker-original.json"
 echo present > "$w/docker-config-state"
else
 echo absent > "$w/docker-config-state"
fi
registered=false
for mode in "${modes[@]}"; do
 test -z "$(timeout 20s docker ps -aq)"
 for side in base candidate; do
  if test -d "$w/active-$side"; then timeout 30s sudo python3 "$support/quiescent.py" "$w/active-$side"; fi
  rm -rf -- "$w/active-$side"
  cp -a "$w/$side-$mode/runtime" "$w/active-$side"
  if ! "$registered"; then
   stage "$side-register" 2m sudo "$w/active-$side/runsc" install --config_file=/etc/docker/daemon.json --runtime="coverage-pilot-$side" --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
  fi
 done
 if ! "$registered"; then
  stage docker-restart 2m sudo systemctl restart docker
  registered=true
 fi
 stage "$mode-runtime-ready" 90s sudo python3 "$support/docker_runtime_ready.py" "$results/$mode-runtime-readiness.json" /etc/docker/daemon.json "coverage-pilot-base=$w/active-base/runsc" "coverage-pilot-candidate=$w/active-candidate/runsc"
 for side in base candidate; do
  python3 "$support/fileset.py" verify "$w/active-$side" "$results/$side-$mode-runtime-manifest.json" "$results/$side-$mode-runtime-before.json"
  for suite in "${suites[@]}"; do run_suite "$side-$mode" "coverage-pilot-$side" "$suite"; done
  python3 "$support/fileset.py" verify "$w/active-$side" "$results/$side-$mode-runtime-manifest.json" "$results/$side-$mode-runtime-after.json"
 done
done
printf '%s\n' 'Capability/volume observations only. No warmup, n24, equivalence or benefit claim. Preserve the original race ELF/link mode and all failed attempts.' > "$results/capability-scope.txt"
