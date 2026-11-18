#!/bin/bash
# AMD64 NORMAL performance study; root approved scope/cost and peer review passed.
set -euo pipefail
if test "${COVERAGE_STUDY_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 150m env COVERAGE_STUDY_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = 7eafb7afe2c0255c98392c2e701ec6aecad22b27
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support="$(cd "$(dirname "$0")" && pwd)"
w="$RUNNER_TEMP/coverage-normal-study"
mkdir -m 700 "$w" "$w/inputs" "$w/inputs/bin" "$w/inputs/archives" "$w/harness" "$w/tools" "$w/tools/bin"
mkdir -p "$RESULT_DIR"
results="$(realpath "$RESULT_DIR")"
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 timeout --kill-after=10s 120s python3 "$support/docker_cleanup.py" "$w" "$results" || rc=1
 git switch --detach "$SOURCE" || rc=1
 git diff --quiet || rc=1
 git diff --cached --quiet || rc=1
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
assert not {'systrap'} & r.keys(),r
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
git switch --detach "$BASE"
# Canonical Bazel image_load paths only; no Docker rebuild or mutable image pull.
for target in //images:benchmark_alpine_load //images/benchmarks/syscallbench:syscallbench_load; do
 stage "image-${target##*:}" 15m "${bazel_cmd[@]}" run "${flags[@]}" "$target"
done
for image in alpine syscallbench; do docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"; done
stage harness-build 15m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles //test/benchmarks/base:startup_test //test/benchmarks/base:syscallbench_test
timeout --kill-after=30s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files 'set(//test/benchmarks/base:startup_test //test/benchmarks/base:syscallbench_test)' > "$results/harness-paths.txt"
stage freeze-harness 2m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json"
stage freeze-harness-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
git show "$BASE:test/benchmarks/defs.bzl" > "$results/benchmark-owning-rule.bzl.txt"
git show "$BASE:Makefile" > "$results/benchmark-owning-Makefile.txt"
for side in base candidate; do
 stage "$side-runtime-archive" 2m tar -C "$w/$side/runtime" -czf "$results/$side-runtime.tar.gz" .
 stage "$side-runtime-freeze" 1m python3 "$support/fileset.py" snapshot "$w/$side/runtime" "$results/$side-frozen-runtime.json"
done
stage benchstat-install 8m bash -c 'cd "$1"; GOBIN="$1/bin" GOWORK=off go install golang.org/x/perf/cmd/benchstat@4febdb082b2ce2a0f9f6f496698aa5a2273279b8' bash "$w/tools"
go version -m "$w/tools/bin/benchstat" > "$results/benchstat-version.txt"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/syscallbench:latest > "$results/frozen-images.txt"
stage bazel-shutdown 2m "${bazel_cmd[@]}" shutdown
# No project builds or source switches after this boundary.
test -z "$(timeout --kill-after=5s 20s sudo docker ps -aq)"
test ! -L /etc/docker/daemon.json
if test -e /etc/docker/daemon.json; then
 sudo cp -a /etc/docker/daemon.json "$w/docker-original.json"
 echo present > "$w/docker-config-state"
else
 echo absent > "$w/docker-config-state"
fi
active="$w/active"
cp -a "$w/base/runtime" "$active"
stage register-runtime 1m sudo "$active/runsc" install --config_file=/etc/docker/daemon.json --runtime=systrap --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
stage restart-docker 2m sudo systemctl restart docker
stage runtime-ready 80s sudo python3 "$support/docker_runtime_ready.py" "$results/runtime-readiness.json" /etc/docker/daemon.json "systrap=$active/runsc"
python3 - "$results" <<'SCHEDULE'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
visits = [{'kind': 'warmup', 'pair': 0, 'side': side} for side in ('base', 'candidate')]
for pair in range(1, 25):
    order = ('base', 'candidate') if pair % 2 else ('candidate', 'base')
    visits.extend({'kind': 'measured', 'pair': pair, 'side': side} for side in order)
(root / 'schedule.json').write_text(json.dumps({'visits': visits, 'measuredPerSide': 24}, indent=2) + '\n')
(root / 'schedule.tsv').write_text(''.join(f"{v['kind']}\t{v['pair']}\t{v['side']}\n" for v in visits))
SCHEDULE
printf '%s\n' 'Frozen BASE harness and full opaque installed normal coverage runtimes; no project build or source switch during all50samepath visits.' > "$results/build-free-boundary.txt"
while IFS=$'\t' read -r kind pair side; do
 stage "$kind-$pair-$side-visit" 90s bash "$support/visit.sh" "$w" "$results" "$support" "$side" "$kind" "$pair"
done < "$results/schedule.tsv"
stage sample-accounting 2m python3 "$support/analyze.py" "$results"
for suite in startup syscall; do
 stage "$suite-benchstat" 1m "$w/tools/bin/benchstat" "$results/base-$suite-clean.txt" "$results/candidate-$suite-clean.txt"
done
printf '%s\n' 'AMD64 NORMAL startup/syscall study only. Race, ARM64 and coverage-report/KCOV-consumption performance are not measured. Original failed pilots and single-observation capability evidence remain separate.' > "$results/measurement-scope.txt"
