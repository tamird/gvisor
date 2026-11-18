#!/bin/bash
set -euo pipefail
if test "${TCP_CATEGORY_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 177m env TCP_CATEGORY_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = b5933c08f6a5730dc174dbe3e377c70c01c6ef20
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/tcp-remaining-categories"
mkdir -m 700 "$w" "$w/base" "$w/base/release" "$w/harness" "$RESULT_DIR/base"
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 timeout --kill-after=2s 18s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --exit-code --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --exit-code --quiet || rc=1
 timeout --kill-after=5s 70s python3 "$support/pilot.py" cleanup "$w" "$RESULT_DIR" "$support" || rc=1
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
test "$(git show -s --format=%P "$SOURCE")" = "$BASE"
printf '%s\n' "$BASE" > "$RESULT_DIR/runtime-source.txt"
printf '%s\n' "$SOURCE" > "$RESULT_DIR/harness-source.txt"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 run apparmor-userns 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
flags='--config=x86_64 -c opt --jobs=3 --nostamp --color=no --curses=no --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
for image in httpd node ruby redis hey util; do
 run "pull-$image" 5m make "${make_args[@]}" "pull-benchmarks_$image"
 sudo docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$RESULT_DIR/image-$image.json"
done
run util-preflight 30s sudo docker run --rm --runtime=runc gvisor.dev/images/benchmarks/util:latest wget --help
run dependency-metadata 2m python3 "$support/dependency_metadata.py" "$RESULT_DIR"
git switch --detach "$BASE"
run base-release 20m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/base/release"
run base-archive 1m tar -C "$w/base/release" -czf "$RESULT_DIR/base/release.tar.gz" .
sha256sum "$RESULT_DIR/base/release.tar.gz" > "$RESULT_DIR/base/release.tar.gz.sha256"
run base-freeze 1m python3 "$support/fileset.py" snapshot "$w/base/release" "$RESULT_DIR/base/frozen-fileset.json"
git switch --detach "$SOURCE"
targets='//test/benchmarks/network:httpd_test //test/benchmarks/network:node_test //test/benchmarks/network:ruby_test //test/benchmarks/database:redis_test'
run harness-build 25m make "${make_args[@]}" build "TARGETS=$targets" OPTIONS=--enable_runfiles
timeout --kill-after=10s 2m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc cquery --config=x86_64 -c opt --jobs=3 --nostamp --enable_runfiles --remote_executor= --remote_cache= --bes_backend= --output=files "set($targets)" > "$RESULT_DIR/harness-paths.txt"
run harness-freeze 2m python3 "$support/freeze_harness.py" "$RESULT_DIR/harness-paths.txt" "$w/harness" "$RESULT_DIR/harness-identity.json" httpd_test node_test ruby_test redis_test
run harness-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$RESULT_DIR/frozen-harness.json"
run bazel-shutdown 2m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown
test ! -L /etc/docker/daemon.json
if test -e /etc/docker/daemon.json; then sudo cp -a /etc/docker/daemon.json "$w/docker-original.json"; echo present > "$w/docker-config-state"; else echo absent > "$w/docker-config-state"; fi
cp -a "$w/base/release" "$w/active"
run register-runtime 1m sudo "$w/active/runsc" install --config_file=/etc/docker/daemon.json --runtime=systrap --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
run restart-docker 1m sudo systemctl restart docker
run runtime-readiness 80s sudo python3 "$support/docker_runtime_ready.py" "$RESULT_DIR/runtime-readiness.json" /etc/docker/daemon.json "systrap=$w/active/runsc"
printf '%s\n' 'Build-free capability boundary; BASE runtime and one repaired frozen harness per category.' > "$RESULT_DIR/build-free-boundary.txt"
run native-controls 14m python3 "$support/pilot.py" native "$w" "$RESULT_DIR" "$support"
# HTTPD concurrency1 is the real Go-selector gate before larger payloads.
for category in httpd node ruby redis; do
 run "capability-$category" 15m python3 "$support/pilot.py" "$category" "$w" "$RESULT_DIR" "$support"
done
run exact-accounting 2m python3 "$support/pilot.py" analyze "$w" "$RESULT_DIR" "$support"
