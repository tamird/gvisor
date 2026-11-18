#!/bin/bash
# Prepared checkpoint8-observation cost screen; root and peer review before publication/launch.
set -euo pipefail
if test "${CHECKPOINT_COST_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=300s 90m env CHECKPOINT_COST_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 3da09be4fe5f629a8b9b86a40d3dd22af27bd089
test "$BASE" = d2c5c08fa3753bcf14ea6015fad547ee8f3d531c
RUNTIME_BASE=1f33fe3003455db178b97f3b43d3a012f1611994
RUNTIME_CANDIDATE=d2c5c08fa3753bcf14ea6015fad547ee8f3d531c
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse HEAD^)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/checkpoint-cost"
results="$RESULT_DIR/checkpoint-cost"
exec_root=$(mktemp -d /tmp/gvisor-checkpoint-exec.XXXXXX)
active="$exec_root/active"
mkdir -m 700 "$w" "$w/harness" "$w/base" "$w/candidate" "$results"
# Only public opaque runtime files live below this traversable execution root.
chmod 711 "$exec_root"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 if test -e "$results/stage-active-group.json";then
  printf '%s\n' 'Stage quiescence unproved; refusing Docker rollback' > "$results/cleanup-incomplete.txt"
  exit 1
 fi
 timeout --kill-after=10s 200s python3 "$support/docker_cleanup.py" "$w" "$results" || rc=1
 if test -e "$w/cleanup-active-group.json";then
  printf '%s\n' 'Cleanup group quiescence unproved; inputs retained' > "$results/cleanup-incomplete.txt"
  exit 1
 fi
 if timeout --kill-after=1s 5s sudo python3 "$support/quiescent.py" "$active";then
  timeout --kill-after=1s 5s rm -rf -- "$exec_root" || rc=1
 else rc=1;fi
 if timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown;then
  timeout --kill-after=5s 15s git switch --detach "$SOURCE" || rc=1
 else
  rc=1
  printf '%s\n' 'Bazel shutdown failed; source inputs retained' > "$results/source-restoration-incomplete.txt"
 fi
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 if test -f "$results/apparmor-original.txt";then
  read -r original_apparmor < "$results/apparmor-original.txt"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original_apparmor" || rc=1
 fi
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local label="$1" cap="$2" rc=0 started
 shift 2
 started=$(python3 -c 'import time;print(time.monotonic_ns())')
 printf '%q ' "$@" >> "$results/commands.txt";printf '\n' >> "$results/commands.txt"
 python3 "$support/stage.py" "$label" "$cap" "$results" "$@" || rc=$?
 timeout --kill-after=1s 5s cat "$results/$label.log" || rc=1
 printf '%s\t%s\t%s\n' "$label" "$(( ($(python3 -c 'import time;print(time.monotonic_ns())')-started)/1000000000 ))" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
sudo -n true
# Fail before image/harness/runtime builds on any inherited search/noexec issue.
stage public-root-execution-preflight 45s python3 "$support/exec_root_probe.py" "$exec_root" "$results/exec-root-preflight.json" gvisor-checkpoint-exec.
git diff --quiet
git diff --cached --quiet
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
test -z "$(timeout 20s sudo docker ps -aq)"
sudo docker info --format '{{json .Runtimes}}' > "$results/runtimes-before.json"
python3 - "$results/runtimes-before.json" <<'NAMES'
import json,sys
assert 'checkpoint-cost' not in json.load(open(sys.argv[1]))
NAMES
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-original.txt"
 stage userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-active.txt"
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
for revision in "$SOURCE" "$BASE";do
 git cat-file -e "$revision^{commit}"
 git cat-file -e "$revision^{tree}"
done
stage owned-group-interruption-preflight 15s python3 "$support/group_preflight.py" "$w" "$results"
# Dispatcher depth2 contains the harness and D2, not necessarily real BASE1f.
if ! git cat-file -e "$RUNTIME_BASE^{tree}" 2>/dev/null;then
 stage fetch-runtime-base 1m git fetch --no-tags --depth=1 origin "$RUNTIME_BASE"
fi
for revision in "$SOURCE" "$BASE" "$RUNTIME_BASE" "$RUNTIME_CANDIDATE";do
 git cat-file -e "$revision^{tree}"
done
printf '{"harnessSource":"%s","harnessParent":"%s","runtimeBase":"%s","runtimeCandidate":"%s"}\n' "$SOURCE" "$BASE" "$RUNTIME_BASE" "$RUNTIME_CANDIDATE" > "$results/runtime-revisions.json"
# No credentials, response headers or signed redirect URLs are recorded.
stage retained-product-route 15s bash -eu -c '
 status=$(curl --silent --show-error --head --location --max-time 10 --output /dev/null --write-out "%{http_code}" https://api.github.com/repos/tamird/gvisor/actions/artifacts/14448787594/zip) || status=transport_error
 printf "%s\n" "$status" > "$1/runtime-artifact-http-status.txt"
 case "$status" in
  200) printf "%s\n" "Transport available; validate retained opaque products before a cold rebuild";exit 1;;
  *) printf "%s\n" "No demonstrated product transport; canonical rebuild required ($status)";;
 esac
' -- "$results"
flags=(--config=x86_64 --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
stage harness-source-format 2m tools/lint.sh --bazel='bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' --bazel-options='--config=x86_64 --jobs=3 --remote_executor= --remote_cache= --bes_backend=' goimports
stage harness-build 10m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles //test/benchmarks/base:startup_test
timeout --kill-after=10s 1m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files 'set(//test/benchmarks/base:startup_test)' > "$results/harness-paths.txt"
stage harness-freeze 1m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json" amd64 startup_test
stage harness-manifest 30s python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
stage harness-archive 1m tar -C "$w/harness" -czf "$results/frozen-harness.tar.gz" .
stage retained-containerd-api 10s sudo ctr version
python3 - "$results/retained-containerd-api.log" <<'CONTAINERD'
from pathlib import Path
import sys
assert Path(sys.argv[1]).read_bytes().count(b'b7a25261e029d68978bbf290df0a62fc819b3c51') == 2, 'Retained native API differs; stop before package changes'
CONTAINERD
stage supported-docker-dependency 17m sudo env DEBIAN_FRONTEND=noninteractive python3 "$support/docker_setup.py" "$w" "$results" "$support"
stage docker-checkpoint-prerequisites 30s sudo python3 "$support/volume.py" preflight "$w" "$results"
git switch --detach "$RUNTIME_BASE"
stage image-alpine 3m "${bazel_cmd[@]}" run "${flags[@]}" //images:benchmark_alpine_load
stage image-nginx 5m make "${make_args[@]}" pull-benchmarks_nginx
for image in alpine nginx;do
 sudo docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"
 test "$(sudo docker image inspect --format '{{.Architecture}}' "gvisor.dev/images/benchmarks/$image:latest")" = amd64
done
touch "$w/native-started"
stage private-store-runtime-preflight 30s sudo python3 "$support/store_preflight.py" "$w" "$results"
stage native-image-input 30s sudo docker run --rm --name checkpoint-cost-image-input --runtime=runc gvisor.dev/images/benchmarks/nginx:latest nginx -t -c /etc/nginx/nginx_gofer.conf

sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/nginx:latest > "$results/frozen-images.txt"
git show "$RUNTIME_BASE:runsc/sandbox/sandbox.go" > "$results/sandbox-launch-source.go.txt"
git show "$RUNTIME_BASE:test/benchmarks/defs.bzl" > "$results/owning-rule.bzl.txt"
git show "$RUNTIME_BASE:Makefile" > "$results/owning-Makefile.txt"
git show "$SOURCE:test/benchmarks/base/startup_test.go" > "$results/owning-startup.go.txt"
git show "$RUNTIME_BASE:pkg/test/dockerutil/container.go" > "$results/owning-docker-checkpoint.go.txt"
build_runtime() {
 local side="$1" revision="$2"
 git switch --detach "$revision"
 mkdir "$w/$side/runtime"
 stage "$side-release-copy" 8m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$side/runtime/"
 stage "$side-runtime-manifest" 30s python3 "$support/fileset.py" snapshot "$w/$side/runtime" "$results/$side-runtime-manifest.json"
 stage "$side-runtime-archive" 1m tar -C "$w/$side/runtime" -czf "$results/$side-runtime.tar.gz" .
}
build_runtime base "$RUNTIME_BASE"
stage bazel-stop-before-first-checkpoint 30s "${bazel_cmd[@]}" shutdown
# Prove the real completion writer/reader before the second cold runtime build.
stage first-base-checkpoint-completion 3m python3 "$support/screen.py" "$w" "$results" "$support" "$exec_root" base-first
test -s "$results/first-base-completion-passed.json"
build_runtime candidate "$RUNTIME_CANDIDATE"
stage bazel-stop-before-remaining-checkpoints 30s "${bazel_cmd[@]}" shutdown
# The same frozen H harness runs all eight; builds stop in each observation phase.
stage checkpoint-remaining-capability-cost 12m python3 "$support/screen.py" "$w" "$results" "$support" "$exec_root" remaining
