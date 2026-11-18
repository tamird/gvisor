#!/bin/bash
# Root-approved report/consume capability pilot; independent executable review complete.
set -euo pipefail
if test "${REPORT_CONSUME_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 117m env REPORT_CONSUME_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = 7eafb7afe2c0255c98392c2e701ec6aecad22b27
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(git rev-parse --verify "$SOURCE^{commit}")" = "$SOURCE"
test "$(git rev-parse --verify "$BASE^{commit}")" = "$BASE"
test "$(git rev-parse "$SOURCE^")" = "$BASE"
git cat-file -e "$SOURCE^{tree}"
git cat-file -e "$BASE^{tree}"
support="$(cd "$(dirname "$0")" && pwd)"
w="$RUNNER_TEMP/coverage-report-consume"
mkdir -m 700 "$w" "$w/inputs" "$w/inputs/bin" "$w/inputs/archives" "$w/harness" "$w/tools" "$w/tools/bin"
mkdir -p "$RESULT_DIR"
results="$(realpath "$RESULT_DIR")"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 timeout --kill-after=5s 80s python3 "$support/workload.py" cleanup "$w" "$results" "$support" || rc=1
 timeout --kill-after=5s 15s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 timeout --kill-after=5s 20s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 timeout --kill-after=5s 20s sudo chown -R "$(id -u):$(id -g)" "$results" || rc=1
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local name="$1" cap="$2" start rc=0
 shift 2
 start=$(date +%s)
 printf '%s\tSTART\t%s\n' "$name" "$start" | tee -a "$results/stages.tsv"
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$name.log" || rc=$?
 printf '%s\tEND\t%s\t%s\n' "$name" "$(( $(date +%s)-start ))" "$rc" | tee -a "$results/stages.tsv"
 return "$rc"
}
command -v bazel
command -v timeout
test -x /usr/bin/time
/usr/bin/time --version > "$RESULT_DIR/time-version.txt"
sudo -n true
# The old go_cov -> //:gopath export transition generates both architecture
# VDSOs, even for this AMD64 pilot. Match the canonical image's ARM64 compiler
# prerequisites without installing its unrelated development/test packages.
if ! test -x /usr/bin/aarch64-linux-gnu-gcc || ! test -x /usr/bin/aarch64-linux-gnu-g++; then
 stage arm64-toolchain-apt-update 5m sudo apt-get update
 stage arm64-toolchain-apt-install 30m sudo env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=120 -o Acquire::https::Timeout=120 install -y --no-install-recommends gcc-aarch64-linux-gnu g++-aarch64-linux-gnu
fi
/usr/bin/aarch64-linux-gnu-gcc --version > "$results/arm64-gcc-version.txt"
/usr/bin/aarch64-linux-gnu-g++ --version > "$results/arm64-gxx-version.txt"
dpkg-query -W gcc-aarch64-linux-gnu g++-aarch64-linux-gnu > "$results/arm64-toolchain-packages.tsv"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 stage apparmor-userns 1m sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
/usr/bin/curl --version > "$results/curl-version.txt"
uname -a > "$results/worker.txt"
lscpu > "$results/cpu.txt"
git diff --exit-code
git diff --cached --exit-code
test "$(git rev-parse "$SOURCE^")" = "$BASE"
cp "$support/curl" "$w/inputs/bin/curl"
chmod 700 "$w/inputs/bin/curl"
cp "$support/go-pins.json" "$w/inputs/go-pins.json"
# Pin and download exactly the original script's source and native bootstrap.
python3 - "$w/inputs/go-pins.json" > "$w/downloads.tsv" <<'PY'
import json,sys
files=json.load(open(sys.argv[1]))['files']
for name in ('go1.27.3.src.tar.gz','go1.27.3.linux-amd64.tar.gz'):
 f=next(f for f in files if f['filename']==name)
 print(name,f['sha256'],sep='\t')
PY
while IFS=$'\t' read -r name digest; do
 stage "download-$name" 4m /usr/bin/curl --proto '=https' --proto-redir '=https' -fsSL --retry 3 --max-time 180 "https://dl.google.com/go/$name" -o "$w/inputs/archives/$name"
 printf '%s  %s\n' "$digest" "$w/inputs/archives/$name" | sha256sum -c -
done < "$w/downloads.tsv"
cp "$w/downloads.tsv" "$results/verified-go-inputs.tsv"
input_hash=$(cat "$w/inputs/bin/curl" "$w/inputs/go-pins.json" | sha256sum | cut -d' ' -f1)
printf '%s\n' "$input_hash" > "$results/verification-input-hash.txt"
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 --jobs=3 --strip=never --nostamp --remote_executor= --remote_cache= --bes_backend=)
# Verify the SOURCE-declared wrappers before any expensive coverage rebuild.
targets=(//test/syscalls:uname_test_runsc_systrap_directfs //test/syscalls:kcov_test_runsc_systrap_directfs)
stage workload-target-query 2m "${bazel_cmd[@]}" query --output=label "set(${targets[*]})"
python3 - "$results/workload-target-query.log" <<'LABELS'
from pathlib import Path
import sys
actual={line for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith('//')}
expected={'//test/syscalls:uname_test_runsc_systrap_directfs','//test/syscalls:kcov_test_runsc_systrap_directfs'}
assert actual==expected,(actual,expected)
LABELS
verify_flags=("--action_env=PATH=$w/inputs/bin:$PATH" "--action_env=VERIFY_COVERAGE_ARCHIVES=$w/inputs/archives" "--action_env=VERIFY_COVERAGE_DOWNLOAD_AUDIT=$results/baseline-download-audit.tsv" "--action_env=VERIFY_COVERAGE_INPUT_SHA256=$input_hash" "--sandbox_add_mount_pair=$w/inputs" "--sandbox_writable_path=$results")
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
printf '%s\n' "${flags[@]}" "${verify_flags[@]}" > "$results/bazel-options.txt"
for side in base candidate; do
 if test "$side" = base; then revision="$BASE"; else revision="$SOURCE"; fi
 git switch --detach "$revision"
 mkdir "$w/$side" "$w/$side/binaries" "$w/$side/runtime"
 extra=()
 if test "$side" = base; then extra=("${verify_flags[@]}"); fi
 for label in //runsc:runsc_coverage //runsc/cmd/sentry:gvisor_sentry_coverage; do
  name="${label##*:}"
  cap=15m
  if test "$side" = base; then cap=20m; fi
  stage "$side-$name-build" "$cap" "${bazel_cmd[@]}" build "${flags[@]}" "${extra[@]}" "$label"
  # cquery must use identical transition/input flags to address the built file.
  path=$(timeout --kill-after=30s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" "${extra[@]}" --output=files "$label")
  test "$(printf '%s\n' "$path" | wc -l)" -eq 1
  cp "$path" "$w/$side/binaries/$name"
  if test "$side" = base; then
   python3 - "$results/baseline-download-audit.tsv" "$w/downloads.tsv" "$name" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); rows=p.read_text().splitlines()
expected={f'https://dl.google.com/go/{name}\t{sha}' for name,sha in (r.split('\t') for r in Path(sys.argv[2]).read_text().splitlines())}
n=1 if sys.argv[3]=='runsc_coverage' else 2
assert len(rows)==2*n and all(rows.count(x)==n for x in expected),(rows,expected,n)
PY
  fi
 done
 stage "$side-release-copy" 15m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$side/runtime/"
 cp --remove-destination "$w/$side/binaries/runsc_coverage" "$w/$side/runtime/runsc"
 cp --remove-destination "$w/$side/binaries/gvisor_sentry_coverage" "$w/$side/runtime/gvisor-bin/gvisor_sentry"
 for slot in runsc gvisor-bin/gvisor_sentry; do
  sha256sum "$w/$side/runtime/$slot" >> "$results/$side-installed.sha256"
  readelf -l "$w/$side/runtime/$slot" > "$results/$side-${slot##*/}-elf.txt"
  if grep -q INTERP "$results/$side-${slot##*/}-elf.txt"; then echo 'Unexpected ELF interpreter' >&2; exit 1; fi
  stage "$side-${slot##*/}-version" 2m "$w/$side/runtime/$slot" --version
 done
 stage "$side-symbolize" 2m "$w/$side/runtime/gvisor-bin/gvisor_sentry" symbolize -all
 grep -q 'pkg/sentry/kernel/' "$results/$side-symbolize.log"
 if grep -Eq '/pkg/(coverage/|ring0/|sleep/|sync/|syncevent/|bpf/|sentry/platform/)' "$results/$side-symbolize.log"; then exit 1; fi
 # Preserve each verified complete runtime before any later arm/harness failure.
 stage "$side-runtime-freeze" 1m python3 "$support/fileset.py" snapshot "$w/$side/runtime" "$results/$side-frozen-runtime.json"
 stage "$side-runtime-archive" 2m tar -C "$w/$side/runtime" -czf "$results/$side-runtime.tar.gz" .
done
git switch --detach "$SOURCE"
stage workload-build 15m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles "${targets[@]}"
timeout --kill-after=10s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files "set(${targets[*]})" > "$results/harness-paths.txt"
stage workload-freeze 3m python3 "$support/freeze.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json"
stage workload-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
# Shared diagnostic wrapper is outside both opaque runtime and SOURCE harness.
cp "$support/report_runtime.py" "$w/tools/report-runtime"
chmod 700 "$w/tools/report-runtime"
sha256sum "$w/tools/report-runtime" > "$results/runtime-wrapper.sha256"
stage bazel-shutdown 2m "${bazel_cmd[@]}" shutdown
printf '%s\n' 'Frozen SOURCE canonical syscall wrappers/runfiles; complete NORMAL runtime at one active path; no project build or source switch during six fixed workloads.' > "$results/build-free-boundary.txt"
stage report-consume 65m python3 "$support/workload.py" run "$w" "$results" "$support"
printf '%s\n' 'Six fixed100-repeat capability/volume observations; no statistical significance, isolated serializer CPU attribution, RACE or ARM claim.' > "$results/scope.txt"
