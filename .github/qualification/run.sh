#!/bin/bash
set -euo pipefail
if test "${READINESS_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 207m env READINESS_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${GITHUB_RUN_ID:?}"
test "$SOURCE" = b5933c08f6a5730dc174dbe3e377c70c01c6ef20
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git show -s --format=%P "$SOURCE")" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
[[ "$GITHUB_RUN_ID" =~ ^[0-9]+$ ]]
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/benchmark-readiness"
mkdir -m 700 "$w" "$w/base" "$w/fixed"
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 # Inclusive timeout+kill budgets sum152s, below the180s outer grace.
 timeout --kill-after=2s 18s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --exit-code --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --exit-code --quiet || rc=1
 timeout --kill-after=5s 70s python3 "$support/control.py" cleanup "$w" "$RESULT_DIR" || rc=1
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 timeout --kill-after=5s 20s sudo chown -R "$(id -u):$(id -g)" "$RESULT_DIR" || rc=1
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
run() {
 local label="$1" cap="$2" rc=0
 shift 2
 printf '%q ' "$@" >> "$RESULT_DIR/commands.txt"; printf '\n' >> "$RESULT_DIR/commands.txt"
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$RESULT_DIR/$label.log" || rc=$?
 printf '%s\t%s\n' "$label" "$rc" >> "$RESULT_DIR/status.tsv"
 return "$rc"
}
for tool in bazel python3 docker timeout; do command -v "$tool"; done
test -z "$(timeout 20s sudo docker ps -aq)"
# Dependency preparation only: cached project images, never source image builds.
flags='--config=x86_64 -c opt --jobs=3 --nostamp --color=no --curses=no --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
for image in nginx httpd node redis hey util; do
 run "pull-$image" 5m make "${make_args[@]}" "pull-benchmarks_$image"
 sudo docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$RESULT_DIR/image-$image.json"
done
run util-preflight 30s sudo docker run --rm --runtime=runc gvisor.dev/images/benchmarks/util:latest wget --help
uname -a > "$RESULT_DIR/worker.txt"
for arm in base fixed; do
 revision="$BASE"
 names=(nginx_test httpd_test node_test)
 cap=25m
 if test "$arm" = fixed; then revision="$SOURCE"; names+=(iperf_test ruby_test); cap=15m; fi
 git switch --detach "$revision"
 targets=()
 for name in "${names[@]}"; do targets+=("//test/benchmarks/network:$name"); done
 run "build-$arm" "$cap" make "${make_args[@]}" build "TARGETS=${targets[*]}" OPTIONS=--enable_runfiles
 timeout --kill-after=10s 2m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc cquery --config=x86_64 -c opt --jobs=3 --nostamp --enable_runfiles --remote_executor= --remote_cache= --bes_backend= --output=files "set(${targets[*]})" > "$RESULT_DIR/paths-$arm.txt"
 run "freeze-$arm" 2m python3 "$support/freeze_harness.py" "$RESULT_DIR/paths-$arm.txt" "$w/$arm" "$RESULT_DIR/harness-$arm.json" "${names[@]}"
 run "manifest-$arm" 2m python3 "$support/fileset.py" snapshot "$w/$arm" "$RESULT_DIR/fileset-$arm.json"
 printf '%s\n' "$revision" > "$RESULT_DIR/harness-source-$arm.txt"
done
run bazel-shutdown 2m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown
status=0
run controls 40m python3 "$support/control.py" run "$w" "$RESULT_DIR" "$support" || status=1
git switch --detach "$SOURCE"
run lint 25m make "${make_args[@]}" lint || status=1
run lint-cc 20m make "${make_args[@]}" lint-cc || status=1
exit "$status"
