#!/bin/bash
# Support-only bounded AMD64 capability and pilot, never statistical qualification.
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 7eafb7afe2c0255c98392c2e701ec6aecad22b27
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(uname -m)" = x86_64
test "${QUAL_CONFIG:-x86_64}" = x86_64
support="$(cd "$(dirname "$0")" && pwd)"
w="$RUNNER_TEMP/coverage-perf-pilot"
mkdir -m 700 "$w"
mkdir "$w/inputs" "$w/inputs/bin" "$w/inputs/archives"
mkdir -p "$RESULT_DIR"
results="$(realpath "$RESULT_DIR")"
config_saved=0
config_existed=0
cleanup() {
 local original_rc=$? cleanup_rc=0
 trap - EXIT
 if test "$config_saved" = 1; then
  if test "$config_existed" = 1; then
   sudo cp --preserve=mode,ownership,timestamps "$w/daemon.json.original" /etc/docker/daemon.json || cleanup_rc=1
  else
   sudo rm -f /etc/docker/daemon.json || cleanup_rc=1
  fi
  timeout --kill-after=15s 1m sudo systemctl reload docker || cleanup_rc=1
 fi
 git switch --detach "$SOURCE" || cleanup_rc=1
 printf '%s\t%s\n' "$original_rc" "$cleanup_rc" > "$results/final-status.tsv"
 if test "$original_rc" = 0 && test "$cleanup_rc" != 0; then original_rc=1; fi
 exit "$original_rc"
}
trap cleanup EXIT
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
docker info > "$results/docker-before.txt"
docker info --format '{{json .Runtimes}}' > "$results/runtime-before.json"
python3 - "$results/runtime-before.json" <<'PYCHECK'
import json,sys
r=json.load(open(sys.argv[1]))
assert not {'coverage-pilot-base','coverage-pilot-candidate'} & r.keys(),r
PYCHECK
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
done
git switch --detach "$SOURCE"
# Canonical Bazel image_load paths only; no Docker rebuild or mutable image pull.
for target in //images:benchmark_alpine_load //images/benchmarks/syscallbench:syscallbench_load; do
 stage "image-${target##*:}" 15m "${bazel_cmd[@]}" run "${flags[@]}" "$target"
done
for image in alpine syscallbench; do docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"; done
stage harness-build 15m "${bazel_cmd[@]}" build "${flags[@]}" //test/benchmarks/base:startup_test //test/benchmarks/base:syscallbench_test
# Preserve the entire daemon configuration and restore it on every exit path.
if sudo test -e /etc/docker/daemon.json; then
 sudo cp --preserve=mode,ownership,timestamps /etc/docker/daemon.json "$w/daemon.json.original"
 config_existed=1
fi
config_saved=1
for side in base candidate; do
 stage "$side-register" 2m sudo "$w/$side/runtime/runsc" install --config_file=/etc/docker/daemon.json --runtime="coverage-pilot-$side" --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
 done
stage docker-reload 1m sudo systemctl reload docker
for side in base candidate; do
 docker info --format '{{json .Runtimes}}' > "$results/docker-runtimes-$side.json"
 grep -q "coverage-pilot-$side" "$results/docker-runtimes-$side.json"
 stage "$side-startup-pilot" 5m make "${make_args[@]}" sudo TARGETS=//test/benchmarks/base:startup_test "ARGS=-runtime=coverage-pilot-$side -test.run=NoTests -test.bench=BenchmarkStartupEmpty -test.benchtime=1x -test.count=1 -test.timeout=4m"
 stage "$side-syscall-pilot" 10m make "${make_args[@]}" sudo TARGETS=//test/benchmarks/base:syscallbench_test "ARGS=-runtime=coverage-pilot-$side -test.run=NoTests -test.bench=Benchmark -test.benchtime=1s -test.count=1 -test.timeout=9m"
done
python3 - "$results" <<'PY'
from pathlib import Path
import json
import math
import re
import sys
p=Path(sys.argv[1])
expected={
 'startup': {'BenchmarkStartupEmpty'},
 'syscall': {
  'BenchmarkSyscallbench/syscall.getpid',
  'BenchmarkSyscallbench/syscall.getpidopt',
  'BenchmarkSyscallUnderSeccomp/cacheable.false',
  'BenchmarkSyscallUnderSeccomp/cacheable.true',
  'BenchmarkForkUnderSeccomp/seccomp.none',
  'BenchmarkForkUnderSeccomp/seccomp.cacheable',
  'BenchmarkForkUnderSeccomp/seccomp.uncacheable',
 },
}
(p/'expected-leaves.json').write_text(json.dumps({k:sorted(v) for k,v in expected.items()},indent=2)+'\n')
for side in ('base','candidate'):
 for suite,count in [('startup',1),('syscall',7)]:
  t=(p/f'{side}-{suite}-pilot.log').read_text()
  assert not re.search(r'(^|\n)(FAIL|--- SKIP:)',t),(side,suite)
  rows=re.findall(r'^(Benchmark\S+)\s+(\d+)\s+([0-9.eE+-]+)\s+ns/op',t,re.M)
  assert len(rows)==count,(side,suite,len(rows),count)
  assert all(int(n)>0 and math.isfinite(float(ns)) and float(ns)>0 for _,n,ns in rows),(side,suite)
  names={re.sub(r'-\d+$','',name) for name,_,_ in rows}
  assert names==expected[suite],(side,suite,names,expected[suite])
(p/'pilot-complete.txt').write_text('AMD64 NORMAL capability/pilot only; no race, ARM64, report/KCOV-consumption, or statistical equivalence claim.\n')
PY
