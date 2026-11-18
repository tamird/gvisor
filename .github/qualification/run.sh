#!/bin/bash
# Full20 functional capability screen; separate source/C++ lint gates.
set -euo pipefail
if test "${VFIO_FUSE_RECOVERY_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=240s 85m env VFIO_FUSE_RECOVERY_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 4f51c3b3a663f39cb1dec661f83406e4f014348a
test "$BASE" = db1c819898c4100058374e37801b5f83ac54d94b
RUNTIME_BASE=1f33fe3003455db178b97f3b43d3a012f1611994
RUNTIME_CANDIDATE=1b37f9cfca65287f8c153314e392d4fbf7c4094b
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse HEAD^)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/vfio-fuse-recovery"
results="$RESULT_DIR/vfio-fuse-recovery"
exec_root=$(mktemp -d /tmp/gvisor-vfio-exec.XXXXXX)
active="$exec_root/active"
mkdir -m 700 "$w" "$w/harness" "$w/base" "$w/candidate" "$results"
# Only public opaque runtime files live below this traversable execution root.
chmod 711 "$exec_root"
cleanup() {
 local rc=$? restoration_ok=1
 trap - EXIT
 trap '' TERM INT
 for marker in "$results/stage-active-group.json" "$results/screen-active-groups.json" "$results/smoke-active-groups.json" "$w/preflight-active-group.json" "$w/nested-owner-active.json";do
  if test -e "$marker";then
   printf '%s\n' 'Owned group quiescence unproved; no competing restoration' > "$results/cleanup-incomplete.txt"
   printf '%s\n' 1 > "$results/final-status.txt"
   exit 1
  fi
 done
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 timeout --kill-after=10s 90s python3 "$support/docker_cleanup.py" "$w" "$results" || { rc=1; restoration_ok=0; }
 if test -e "$results/cleanup-active-group.json";then
  printf '%s\n' 'Cleanup group quiescence unproved; inputs retained' > "$results/cleanup-incomplete.txt"
  printf '%s\n' 1 > "$results/final-status.txt"
  exit 1
 fi
 if test "$restoration_ok" -eq 1 && timeout --kill-after=1s 5s sudo python3 "$support/quiescent.py" "$active";then
  timeout --kill-after=1s 5s rm -rf -- "$exec_root" || rc=1
 else rc=1;fi
 timeout --kill-after=5s 15s git switch --detach "$SOURCE" || rc=1
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
 local label="$1" cap="$2" rc=0 start
 shift 2
 start=$(python3 -c 'import time;print(time.monotonic_ns())')
 printf '%q ' "$@" >> "$results/commands.txt";printf '\n' >> "$results/commands.txt"
 printf '%s START\n' "$label"
 python3 "$support/stage.py" "$label" "$cap" "$results" "$@" || rc=$?
 timeout --kill-after=1s 5s cat "$results/$label.log" || rc=1
 printf '%s\t%s\t%s\n' "$label" "$(( ($(python3 -c 'import time;print(time.monotonic_ns())')-start)/1000000 ))" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
sudo -n true
stage owned-group-control 30s python3 "$support/group_preflight.py" "$w" "$results"
# Fail before image/harness/runtime builds on any inherited search/noexec issue.
stage public-root-execution-preflight 45s python3 "$support/exec_root_probe.py" "$exec_root" "$results/exec-root-preflight.json" gvisor-vfio-exec.
git diff --quiet
git diff --cached --quiet
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
test -z "$(timeout 20s sudo docker ps -aq)"
sudo docker info --format '{{json .Runtimes}}' > "$results/runtimes-before.json"
stage docker-version 10s bash -c 'sudo docker version --format "{{json .Server.Version}}" > "$1"' _ "$results/docker-server-version.json"
python3 - "$results/docker-server-version.json" <<'VERSION'
import json,sys
actual=json.load(open(sys.argv[1]))
assert actual=='28.0.4', ('Unqualified Docker reload version',actual)
VERSION
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
stage runtime-object-fetch 2m git fetch --no-tags --no-write-fetch-head origin "$RUNTIME_BASE" "$RUNTIME_CANDIDATE"
for revision in "$RUNTIME_BASE" "$RUNTIME_CANDIDATE";do test "$(git rev-parse --verify "$revision^{commit}")" = "$revision";git cat-file -e "$revision^{tree}";done
stage image-alpine 3m "${bazel_cmd[@]}" run "${flags[@]}" //images:benchmark_alpine_load
stage image-rubydev 5m make "${make_args[@]}" pull-benchmarks_rubydev

for image in alpine rubydev;do
 sudo docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"
 test "$(sudo docker image inspect --format '{{.Architecture}}' "gvisor.dev/images/benchmarks/$image:latest")" = amd64
done
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/rubydev:latest > "$results/frozen-images.txt"
stage harness-build 10m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles //test/benchmarks/base:startup_test //test/benchmarks/fs:rubydev_test //test/benchmarks/fs:bazel_test
timeout --kill-after=10s 1m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files 'set(//test/benchmarks/base:startup_test //test/benchmarks/fs:rubydev_test)' > "$results/harness-paths.txt"
stage harness-freeze 1m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json" amd64 startup_test rubydev_test
stage harness-manifest 30s python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
# Ruby changed intentionally; prior native Ruby outcomes remain historical.
# Compare the complete unchanged startup product and exact image identities.
# Native observations are fresh: prior native runtime identity was not captured.
cmp "$results/frozen-images.txt" "$support/prior-frozen-images.txt"
python3 - "$w/harness" "$support/prior-frozen-harness.json" "$results/prior-harness-products-match.json" "$support/prior-source4f-ruby-product.json" <<'STARTUP'
import hashlib,json,stat,sys
from pathlib import Path
root=Path(sys.argv[1]);frozen=json.loads(Path(sys.argv[2]).read_text())['actual']
expected={k:v for k,v in frozen.items() if k=='startup_test' or k.startswith('startup_test.runfiles/')}
actual={}
for path in root.rglob('*'):
 if path.is_file():
  name=str(path.relative_to(root))
  if name=='startup_test' or name.startswith('startup_test.runfiles/'):actual[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'size':path.stat().st_size,'mode':stat.S_IMODE(path.stat().st_mode)}
assert actual==expected and len(expected)==4
ruby_expected=json.loads(Path(sys.argv[4]).read_text())
ruby_actual={}
for path in root.rglob('*'):
 if path.is_file():
  name=str(path.relative_to(root))
  if name=='rubydev_test' or name.startswith('rubydev_test.runfiles/'):
   ruby_actual[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'size':path.stat().st_size,'mode':stat.S_IMODE(path.stat().st_mode)}
assert ruby_actual==ruby_expected and len(ruby_expected)==5
Path(sys.argv[3]).write_text(json.dumps({'sourceHarnessChanged':True,'priorStartupProductMatches':True,'matchingStartupMembers':len(expected),'matchingRubyMembers':len(ruby_expected),'nativeResultsReused':False},indent=2)+'\n')
STARTUP
stage harness-archive 1m tar -C "$w/harness" -czf "$results/frozen-harness.tar.gz" .
git show "$SOURCE:runsc/sandbox/sandbox.go" > "$results/sandbox-launch-source.go.txt"
git show "$SOURCE:test/benchmarks/defs.bzl" > "$results/owning-rule.bzl.txt"
git show "$SOURCE:Makefile" > "$results/owning-Makefile.txt"
git show "$SOURCE:test/benchmarks/fs/fsbench/fsbench.go" > "$results/owning-fsbench.go.txt"
git show "$SOURCE:test/benchmarks/fs/rubydev_test.go" > "$results/owning-rubydev.go.txt"
# Capability-only native gate first, before repeating any full runtime build.
stage new-native-mount-gates 4m python3 "$support/screen.py" native "$w" "$results" "$support" "$exec_root"
stage image-basic-alpine 3m make "${make_args[@]}" pull-basic_alpine
sudo docker image inspect gvisor.dev/images/basic/alpine:latest > "$results/image-basic-alpine.json"
test "$(sudo docker image inspect --format '{{.Architecture}}' gvisor.dev/images/basic/alpine:latest)" = amd64
git diff --quiet
git diff --cached --quiet
for side in candidate base;do
 revision="$RUNTIME_BASE";if test "$side" = candidate;then revision="$RUNTIME_CANDIDATE";fi
 git switch --detach "$revision"
 mkdir "$w/$side/runtime"
 cap=8m
 stage "$side-release-copy" "$cap" make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$side/runtime/"
 stage "$side-runtime-manifest" 30s python3 "$support/fileset.py" snapshot "$w/$side/runtime" "$results/$side-runtime-manifest.json"
 python3 "$support/fileset.py" verify "$w/$side/runtime" "$support/prior-$side-runtime.json" "$results/$side-reproduced-prior-runtime.json"
 stage "$side-runtime-archive" 1m tar -C "$w/$side/runtime" -czf "$results/$side-runtime.tar.gz" .
done
git switch --detach "$SOURCE"
stage native-owner-smoke 5m python3 "$support/smoke.py" "$w" "$results" "$support" native runc
stage source-owner-smokes 6m python3 "$support/screen.py" smoke "$w" "$results" "$support" "$exec_root"
stage bazel-shutdown 1m "${bazel_cmd[@]}" shutdown
# All runtime builds/switches end before source observations. Native gates above are not timing controls.
stage missing-source-screen 12m python3 "$support/screen.py" sources "$w" "$results" "$support" "$exec_root"
