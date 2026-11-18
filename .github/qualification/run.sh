#!/bin/bash
set -euo pipefail
if test "${TCP_IPERF_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 195m env TCP_IPERF_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = f9446ddf973def4ce653acbb098532c2b1eb0ef0
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git show -s --format=%P "$SOURCE")" = "$BASE"
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/tcp-iperf-study"
mkdir -m 700 "$w" "$w/harness" "$w/tools" "$w/tools/bin"
results="$RESULT_DIR/iperf-study"
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
 printf '%q ' "$@" >> "$results/commands.txt"; printf '\n' >> "$results/commands.txt"
 timeout --signal=TERM --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$label.log" || rc=$?
 printf '%s\t%s\n' "$label" "$rc" >> "$results/status.tsv"
 return "$rc"
}
for tool in bazel python3 go docker timeout tar; do command -v "$tool"; done
test "$(df -Pk "$RUNNER_TEMP" | awk 'NR==2 {print $4}')" -ge 12582912
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
go version > "$results/host-go-version.txt"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 run apparmor-userns 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
flags='--config=x86_64 -c opt --jobs=3 --nostamp --color=no --curses=no --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
for arm in base candidate; do
 revision="$BASE"; cap=15m
 if test "$arm" = candidate; then revision="$SOURCE"; cap=5m; fi
 git switch --detach "$revision"
 mkdir -m 700 "$w/$arm" "$w/$arm/release" "$results/$arm"
 run "$arm-release" "$cap" make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$arm/release"
 run "$arm-archive" 1m tar -C "$w/$arm/release" -czf "$results/$arm/release.tar.gz" .
 run "$arm-freeze" 1m python3 "$support/fileset.py" snapshot "$w/$arm/release" "$results/$arm/frozen-fileset.json"
done
# One BASE harness, unchanged throughout all measured source visits.
git switch --detach "$BASE"
run harness-build 15m make "${make_args[@]}" build TARGETS=//test/benchmarks/network:iperf_test OPTIONS=--enable_runfiles
timeout --kill-after=30s 1m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc cquery --config=x86_64 -c opt --jobs=3 --nostamp --enable_runfiles --remote_executor= --remote_cache= --bes_backend= --output=files //test/benchmarks/network:iperf_test > "$results/harness-paths.txt"
run harness-freeze 1m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json" iperf_test
run harness-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
git show "$BASE:test/benchmarks/defs.bzl" > "$results/benchmark-owning-rule.bzl.txt"
git show "$BASE:Makefile" > "$results/benchmark-owning-Makefile.txt"
printf '%s\n' "$BASE" > "$results/harness-source.txt"
run iperf-image 5m make "${make_args[@]}" pull-benchmarks_iperf
sudo docker image inspect gvisor.dev/images/benchmarks/iperf:latest > "$results/image-inspect.json"
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/iperf:latest > "$results/image-identity.txt"
run benchstat-install 8m bash -c 'cd "$1"; GOBIN="$1/bin" GOWORK=off go install golang.org/x/perf/cmd/benchstat@4febdb082b2ce2a0f9f6f496698aa5a2273279b8' bash "$w/tools"
go version -m "$w/tools/bin/benchstat" > "$results/benchstat-version.txt"
run bazel-shutdown 2m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown
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
run register-runtime 1m sudo "$active/runsc" install --config_file=/etc/docker/daemon.json --runtime=systrap --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
run restart-docker 1m sudo systemctl restart docker
run runtime-readiness 80s sudo python3 "$support/docker_runtime_ready.py" "$results/runtime-readiness.json" /etc/docker/daemon.json "systrap=$active/runsc"
python3 - "$results" <<'SCHEDULE'
import json
from pathlib import Path
import sys
root=Path(sys.argv[1])
visits=[{'kind':'warmup','number':0,'arm':arm} for arm in ('base','candidate')]
for pair in range(1,25):
 order=('base','candidate') if pair%2 else ('candidate','base')
 visits.extend({'kind':'measured','number':pair,'arm':arm} for arm in order)
(root/'schedule.json').write_text(json.dumps({'visits':visits,'samplesPerSource':24,'iterationCount':1048576,'activePathSame':True,'harnessSource':'1f33fe3003455db178b97f3b43d3a012f1611994'},indent=2)+'\n')
(root/'schedule.tsv').write_text(''.join(f"{visit['kind']}\t{visit['number']}\t{visit['arm']}\n" for visit in visits))
SCHEDULE
printf '%s\n' 'Frozen: no project build, source switch or dependency installation during visits.' > "$results/build-free-boundary.txt"
while IFS=$'\t' read -r kind number arm; do
 run "$kind-$number-$arm-visit" 180s bash "$support/visit.sh" "$w" "$results" "$support" "$arm" "$kind" "$number"
done < "$results/schedule.tsv"
run sample-accounting 2m python3 "$support/analyze.py" "$results" "$support/expected-leaves.json"
run raw-benchstat 1m "$w/tools/bin/benchstat" "$results/base-clean.txt" "$results/candidate-clean.txt"
