#!/bin/bash
# Prepared missing-only19-observation screen; root and peer review before publication/launch.
set -euo pipefail
if test "${VFIO_MISSING_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=180s 60m env VFIO_MISSING_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 1b37f9cfca65287f8c153314e392d4fbf7c4094b
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse HEAD^)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/vfio-missing-capability"
results="$RESULT_DIR/vfio-missing-capability"
exec_root=$(mktemp -d /tmp/gvisor-vfio-exec.XXXXXX)
active="$exec_root/active"
mkdir -m 700 "$w" "$w/harness" "$w/base" "$w/candidate" "$results"
# Only public opaque runtime files live below this traversable execution root.
chmod 711 "$exec_root"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 timeout --kill-after=10s 90s python3 "$support/docker_cleanup.py" "$w" "$results" || rc=1
 if timeout --kill-after=1s 5s sudo python3 "$support/quiescent.py" "$active";then
  timeout --kill-after=1s 5s rm -rf -- "$exec_root" || rc=1
 else rc=1;fi
 timeout --kill-after=5s 15s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 if test -f "$results/apparmor-original.txt";then
  read -r original_apparmor < "$results/apparmor-original.txt"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original_apparmor" || rc=1
 fi
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local label="$1" cap="$2" rc=0 started
 shift 2
 started=$(date +%s)
 printf '%q ' "$@" >> "$results/commands.txt";printf '\n' >> "$results/commands.txt"
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$label.log" || rc=$?
 printf '%s\t%s\t%s\n' "$label" "$(( $(date +%s)-started ))" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
mkdir "$results/reused-native-controls-44241613500"
cp "$support"/prior-* "$results/reused-native-controls-44241613500/"
python3 - "$results/reused-native-controls-44241613500" <<'PRIOR'
import base64,hashlib,json,sys
from pathlib import Path
r=Path(sys.argv[1]);p=json.loads((r/"prior-proof.json").read_text())
logs=json.loads((r/"prior-native-logs.json").read_text())
assert set(logs)=={"prior-native-startup.log","prior-native-ruby.log"}
for name,encoded in logs.items():(r/name).write_bytes(base64.b64decode(encoded,validate=True))
assert p["source"]=="1b37f9cfca65287f8c153314e392d4fbf7c4094b" and p["base"]=="1f33fe3003455db178b97f3b43d3a012f1611994" and p["run"]==44241613500
for name,digest in p["files"].items():assert hashlib.sha256((r/name).read_bytes()).hexdigest()==digest,name
PRIOR
sudo -n true
# Fail before image/harness/runtime builds on any inherited search/noexec issue.
stage public-root-execution-preflight 45s python3 "$support/exec_root_probe.py" "$exec_root" "$results/exec-root-preflight.json" gvisor-vfio-exec.
git diff --quiet
git diff --cached --quiet
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
test -z "$(timeout 20s sudo docker ps -aq)"
sudo docker info --format '{{json .Runtimes}}' > "$results/runtimes-before.json"
python3 - "$results/runtimes-before.json" <<'NAMES'
import json,sys
assert 'vfio-gofer-pilot' not in json.load(open(sys.argv[1]))
NAMES
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-original.txt"
 stage userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-active.txt"
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
git switch --detach "$BASE"
stage image-alpine 3m "${bazel_cmd[@]}" run "${flags[@]}" //images:benchmark_alpine_load
stage image-rubydev 5m make "${make_args[@]}" pull-benchmarks_rubydev
for image in alpine rubydev;do
 sudo docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"
 test "$(sudo docker image inspect --format '{{.Architecture}}' "gvisor.dev/images/benchmarks/$image:latest")" = amd64
done
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/rubydev:latest > "$results/frozen-images.txt"
stage harness-build 10m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles //test/benchmarks/base:startup_test //test/benchmarks/fs:rubydev_test
timeout --kill-after=10s 1m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files 'set(//test/benchmarks/base:startup_test //test/benchmarks/fs:rubydev_test)' > "$results/harness-paths.txt"
stage harness-freeze 1m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json" amd64 startup_test rubydev_test
stage harness-manifest 30s python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
# Prior native observations apply only to these exact image and full harness identities.
cmp "$results/frozen-images.txt" "$support/prior-frozen-images.txt"
python3 "$support/fileset.py" verify "$w/harness" "$support/prior-frozen-harness.json" "$results/reproduced-prior-harness.json"
stage harness-archive 1m tar -C "$w/harness" -czf "$results/frozen-harness.tar.gz" .
git show "$BASE:runsc/sandbox/sandbox.go" > "$results/sandbox-launch-source.go.txt"
git show "$BASE:test/benchmarks/defs.bzl" > "$results/owning-rule.bzl.txt"
git show "$BASE:Makefile" > "$results/owning-Makefile.txt"
git show "$BASE:test/benchmarks/fs/fsbench/fsbench.go" > "$results/owning-fsbench.go.txt"
git show "$BASE:test/benchmarks/fs/rubydev_test.go" > "$results/owning-rubydev.go.txt"
for side in base candidate;do
 revision="$BASE";if test "$side" = candidate;then revision="$SOURCE";fi
 git switch --detach "$revision"
 mkdir "$w/$side/runtime"
 stage "$side-release-copy" 8m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$side/runtime/"
 stage "$side-runtime-manifest" 30s python3 "$support/fileset.py" snapshot "$w/$side/runtime" "$results/$side-runtime-manifest.json"
 python3 "$support/fileset.py" verify "$w/$side/runtime" "$support/prior-$side-runtime.json" "$results/$side-reproduced-prior-runtime.json"
 stage "$side-runtime-archive" 1m tar -C "$w/$side/runtime" -czf "$results/$side-runtime.tar.gz" .
done
stage bazel-shutdown 1m "${bazel_cmd[@]}" shutdown
# No further builds or source switches during any observed visit.
stage missing-native-and-source-screen 12m python3 "$support/screen.py" "$w" "$results" "$support" "$exec_root"
