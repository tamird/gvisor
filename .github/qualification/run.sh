#!/bin/bash
set -euo pipefail
if test "${TCP_CATEGORY_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 177m env TCP_CATEGORY_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
DIAGNOSTIC_SOURCE=aed925f04bead9b3d6917fbab5aac656af4b7ef8
test "$SOURCE" = "$DIAGNOSTIC_SOURCE"
test "$BASE" = b5933c08f6a5730dc174dbe3e377c70c01c6ef20
RUNTIME_BASE=1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/tcp-remaining-recovery"
mkdir -m 700 "$w" "$w/base" "$w/base/release" "$w/harness" "$RESULT_DIR/base"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 timeout --kill-after=2s 18s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --exit-code --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --exit-code --quiet || rc=1
 timeout --kill-after=5s 90s python3 "$support/pilot.py" cleanup "$w" "$RESULT_DIR" "$support" || rc=1
 timeout --kill-after=5s 5s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 timeout --kill-after=5s 10s sudo chown -R "$(id -u):$(id -g)" "$RESULT_DIR" || rc=1
 exit "$rc"
}
exec_root=$(mktemp -d /tmp/gvisor-tcp-exec.XXXXXX)
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
 run apparmor-userns 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
flags='--config=x86_64 -c opt --jobs=3 --nostamp --color=no --curses=no --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
run public-root-preflight 45s python3 "$support/exec_root_probe.py" "$exec_root" "$RESULT_DIR/exec-root-preflight.json" gvisor-tcp-exec.
# The dispatcher fetches SOURCE and its immediate parent, not this grandparent.
run fetch-runtime-base 1m git fetch --no-tags --depth=1 origin "$RUNTIME_BASE"
test "$(git rev-parse --verify "$RUNTIME_BASE^{commit}")" = "$RUNTIME_BASE"
git cat-file -e "$RUNTIME_BASE^{tree}"
for image in httpd node ruby redis hey util; do
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
run dependency-metadata 2m python3 "$support/dependency_metadata.py" "$RESULT_DIR"
git switch --detach "$RUNTIME_BASE"
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
cp -a "$w/base/release" "$exec_root/active"
run public-runtime-fileset 1m python3 "$support/fileset.py" verify "$exec_root/active" "$RESULT_DIR/base/frozen-fileset.json" "$RESULT_DIR/public-runtime-fileset.json"
run mapped-runtime-access 1m python3 "$support/runtime_access.py" "$exec_root/active" "$exec_root" "$RESULT_DIR/runtime-access.json"
run register-runtime 1m sudo "$exec_root/active/runsc" install --config_file=/etc/docker/daemon.json --runtime=systrap --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false
run restart-docker 1m sudo systemctl restart docker
run runtime-readiness 80s sudo python3 "$support/docker_runtime_ready.py" "$RESULT_DIR/runtime-readiness.json" /etc/docker/daemon.json "systrap=$exec_root/active/runsc"
run configured-runtime-preflight 1m sudo docker run --rm --runtime=systrap gvisor.dev/images/benchmarks/util:latest /bin/true
printf '%s\n' 'Build-free capability boundary; BASE runtime and one repaired frozen harness per category.' > "$RESULT_DIR/build-free-boundary.txt"
printf '%s\n' 'Prior44238122262 retained as execution-only/finite-summary evidence. This recovery rechecks all70 Sentry leaves and four lightweight native controls for missing raw request validity, plus one high-concurrency native diagnostic control. No statistical extension.' > "$RESULT_DIR/prior-evidence-scope.txt"
status=0
run native-controls 14m python3 "$support/pilot.py" native "$w" "$RESULT_DIR" "$support" || status=1
# Actual native producers must create parseable raw records before costlier groups.
python3 - "$RESULT_DIR/native-evidence.json" <<'RAW_GATE'
import json,sys
from pathlib import Path
records=json.loads(Path(sys.argv[1]).read_text())
assert len(records)==4 and all(record.get('rawProof',{}).get('records') for record in records),records
RAW_GATE
run native-high 3m python3 "$support/pilot.py" native-high "$w" "$RESULT_DIR" "$support" || status=1
python3 - "$RESULT_DIR/native-high-evidence.json" <<'HIGH_RAW_GATE'
import json,sys
from pathlib import Path
records=json.loads(Path(sys.argv[1]).read_text())
assert len(records)==1 and records[0].get('rawProof',{}).get('records'),records
HIGH_RAW_GATE
run capability-httpd 12m python3 "$support/pilot.py" httpd "$w" "$RESULT_DIR" "$support" || status=1
for category in node ruby redis; do
 run "capability-$category" 15m python3 "$support/pilot.py" "$category" "$w" "$RESULT_DIR" "$support" || status=1
done
run exact-accounting 2m python3 "$support/pilot.py" analyze "$w" "$RESULT_DIR" "$support" || status=1
exit "$status"
