#!/bin/bash
set -euo pipefail
if test "${HTTPD_DIAG_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 67m env HTTPD_DIAG_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
DIAGNOSTIC_SOURCE=aed925f04bead9b3d6917fbab5aac656af4b7ef8
test "$SOURCE" = "$DIAGNOSTIC_SOURCE"
test "$BASE" = b5933c08f6a5730dc174dbe3e377c70c01c6ef20
RUNTIME_BASE=1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/tcp-httpd-diagnosis"
mkdir -m 700 "$w" "$w/base" "$w/base/release" "$w/harness" "$RESULT_DIR/base"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 timeout --kill-after=2s 18s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --exit-code --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --exit-code --quiet || rc=1
 timeout --kill-after=5s 90s python3 "$support/diagnose.py" cleanup "$w" "$RESULT_DIR" "$support" || rc=1
 if test -f "$w/apparmor-userns-original"; then
  read -r original_apparmor < "$w/apparmor-userns-original"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original_apparmor" > "$RESULT_DIR/apparmor-userns-restored.txt" || rc=1
 fi
 timeout --kill-after=5s 5s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 timeout --kill-after=2s 8s sudo chown -R "$(id -u):$(id -g)" "$RESULT_DIR" || rc=1
 exit "$rc"
}
exec_root=$(mktemp -d /tmp/gvisor-httpd-exec.XXXXXX)
chmod 0711 "$exec_root"
printf '%s\n' "$exec_root" > "$w/public-runtime-root"
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
printf '%s\n' "$RUNTIME_BASE" > "$RESULT_DIR/runtime-source.txt"
printf '%s\n' "$SOURCE" > "$RESULT_DIR/harness-source.txt"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$w/apparmor-userns-original"
 cp "$w/apparmor-userns-original" "$RESULT_DIR/apparmor-userns-original.txt"
 read -r original_apparmor < "$w/apparmor-userns-original"
 test "$original_apparmor" = 0 || test "$original_apparmor" = 1
 run apparmor-userns-prerequisite 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$RESULT_DIR/apparmor-userns-active.txt"
fi
flags='--config=x86_64 -c opt --jobs=3 --nostamp --color=no --curses=no --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
run public-root-preflight 45s python3 "$support/exec_root_probe.py" "$exec_root" "$RESULT_DIR/exec-root-preflight.json" gvisor-httpd-exec.
# The dispatcher fetches SOURCE and its immediate parent, not this grandparent.
run fetch-runtime-base 1m git fetch --no-tags --depth=1 origin "$RUNTIME_BASE"
test "$(git rev-parse --verify "$RUNTIME_BASE^{commit}")" = "$RUNTIME_BASE"
git cat-file -e "$RUNTIME_BASE^{tree}"
for image in httpd hey util; do
 run "pull-$image" 5m make "${make_args[@]}" "pull-benchmarks_$image"
 sudo docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$RESULT_DIR/image-$image.json"
done
python3 - "$support/reused-image-identities.json" "$RESULT_DIR" <<'IMAGES'
import json,sys
from pathlib import Path
expected=json.loads(Path(sys.argv[1]).read_text())
results=Path(sys.argv[2])
for name,identifier in expected.items():
 assert json.loads((results/f'image-{name}.json').read_text())[0]['Id']==identifier,name
IMAGES
run util-preflight 30s sudo docker run --rm --runtime=runc gvisor.dev/images/benchmarks/util:latest wget --help
run static-configuration 2m python3 "$support/diagnose.py" static "$w" "$RESULT_DIR" "$support"
git switch --detach "$RUNTIME_BASE"
run base-release 15m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/base/release"
run base-archive 1m tar -C "$w/base/release" -czf "$RESULT_DIR/base/release.tar.gz" .
sha256sum "$RESULT_DIR/base/release.tar.gz" > "$RESULT_DIR/base/release.tar.gz.sha256"
run base-freeze 1m python3 "$support/fileset.py" snapshot "$w/base/release" "$RESULT_DIR/base/frozen-fileset.json"
git switch --detach "$SOURCE"
targets=//test/benchmarks/network:httpd_test
run harness-build 10m make "${make_args[@]}" build "TARGETS=$targets" OPTIONS=--enable_runfiles
timeout --kill-after=10s 1m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc cquery --config=x86_64 -c opt --jobs=3 --nostamp --enable_runfiles --remote_executor= --remote_cache= --bes_backend= --output=files "set($targets)" > "$RESULT_DIR/harness-paths.txt"
run harness-freeze 1m python3 "$support/freeze_harness.py" "$RESULT_DIR/harness-paths.txt" "$w/harness" "$RESULT_DIR/harness-identity.json" httpd_test
run harness-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$RESULT_DIR/frozen-harness.json"
run bazel-shutdown 1m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown
test ! -L /etc/docker/daemon.json
if test -e /etc/docker/daemon.json; then sudo cp -a /etc/docker/daemon.json "$w/docker-original.json"; echo present > "$w/docker-config-state"; else echo absent > "$w/docker-config-state"; fi
cp -a "$w/base/release" "$exec_root/active"
run public-runtime-fileset 45s python3 "$support/fileset.py" verify "$exec_root/active" "$RESULT_DIR/base/frozen-fileset.json" "$RESULT_DIR/public-runtime-fileset.json"
run mapped-runtime-access 45s python3 "$support/runtime_access.py" "$exec_root/active" "$exec_root" "$RESULT_DIR/runtime-access.json"
run register-runtime 45s sudo "$exec_root/active/runsc" install --config_file=/etc/docker/daemon.json --runtime=systrap --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
run restart-docker 45s sudo systemctl restart docker
run runtime-readiness 80s sudo python3 "$support/docker_runtime_ready.py" "$RESULT_DIR/runtime-readiness.json" /etc/docker/daemon.json "systrap=$exec_root/active/runsc"
run configured-runtime-preflight 45s sudo docker run --rm --runtime=systrap gvisor.dev/images/benchmarks/util:latest /bin/true
run diagnostic-visits 6m python3 "$support/diagnose.py" run "$w" "$RESULT_DIR" "$support"
