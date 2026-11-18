#!/bin/bash
# Prepared ordinary-gofer capability/cost pilot; root approval before launch.
set -euo pipefail
if test "${VFIO_PERF_PILOT_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=180s 75m env VFIO_PERF_PILOT_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 1b37f9cfca65287f8c153314e392d4fbf7c4094b
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse HEAD^)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/vfio-perf-capability"
results="$RESULT_DIR/vfio-perf-capability"
exec_root="$RUNNER_TEMP/vfio-gofer-exec"
active="$exec_root/active"
mkdir -m 700 "$w" "$w/harness" "$w/base" "$w/candidate" "$results"
# Only public opaque runtime files live below this traversable execution root.
mkdir -m 711 "$exec_root"
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
 timeout --kill-after=5s 20s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
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
sudo -n true
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
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then stage userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0;fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
git switch --detach "$BASE"
stage image-alpine 10m "${bazel_cmd[@]}" run "${flags[@]}" //images:benchmark_alpine_load
stage image-rubydev 15m make "${make_args[@]}" pull-benchmarks_rubydev
for image in alpine rubydev;do
 sudo docker image inspect "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"
 test "$(sudo docker image inspect --format '{{.Architecture}}' "gvisor.dev/images/benchmarks/$image:latest")" = amd64
done
sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/rubydev:latest > "$results/frozen-images.txt"
stage harness-build 25m "${bazel_cmd[@]}" build "${flags[@]}" --enable_runfiles //test/benchmarks/base:startup_test //test/benchmarks/fs:rubydev_test
timeout --kill-after=10s 2m "${bazel_cmd[@]}" cquery "${flags[@]}" --enable_runfiles --output=files 'set(//test/benchmarks/base:startup_test //test/benchmarks/fs:rubydev_test)' > "$results/harness-paths.txt"
stage harness-freeze 2m python3 "$support/freeze_harness.py" "$results/harness-paths.txt" "$w/harness" "$results/harness-identity.json" amd64 startup_test rubydev_test
stage harness-manifest 1m python3 "$support/fileset.py" snapshot "$w/harness" "$results/frozen-harness.json"
# Prior native observations apply only to these exact image and full harness identities.
cmp "$results/frozen-images.txt" "$support/prior-frozen-images.txt"
python3 "$support/fileset.py" verify "$w/harness" "$support/prior-frozen-harness.json" "$results/reproduced-prior-harness.json"
stage harness-archive 2m tar -C "$w/harness" -czf "$results/frozen-harness.tar.gz" .
git show "$BASE:runsc/sandbox/sandbox.go" > "$results/sandbox-launch-source.go.txt"
git show "$BASE:test/benchmarks/defs.bzl" > "$results/owning-rule.bzl.txt"
git show "$BASE:Makefile" > "$results/owning-Makefile.txt"
git show "$BASE:test/benchmarks/fs/fsbench/fsbench.go" > "$results/owning-fsbench.go.txt"
git show "$BASE:test/benchmarks/fs/rubydev_test.go" > "$results/owning-rubydev.go.txt"
run_suite() {
 local label="$1" runtime="$2" suite="$3" binary selector
 binary=startup_test;selector='^BenchmarkStartupEmpty$'
 if test "$suite" = ruby;then binary=rubydev_test;selector='^BenchmarkRubyNoOpTest$/^page_cache[.]dirty$/^filesystem[.]bindfs$';fi
 local runfiles="$w/harness/$binary.runfiles"
 mkdir -p "$w/bench-tmp" "$results/$label-$suite-output"
 stage "$label-$suite" 3m sudo env RUNTIME="$runtime" GOMAXPROCS=4 RUNFILES_DIR="$runfiles" TEST_SRCDIR="$runfiles" TEST_WORKSPACE=_main TMPDIR="$w/bench-tmp" TEST_TMPDIR="$w/bench-tmp" TEST_UNDECLARED_OUTPUTS_DIR="$results/$label-$suite-output" "$w/harness/$binary" "-runtime=$runtime" -test.run=NoTests "-test.bench=$selector" -test.benchtime=1x -test.count=1 -test.cpu=4
 python3 "$support/validate.py" "$results/$label-$suite.log" "$results/$label-$suite.json" "$suite"
}
# Native inputs already passed in the failed receipt; exact image and complete
# frozen-harness reproduction above is mandatory before reusing that evidence.
for side in base candidate;do
 revision="$BASE";if test "$side" = candidate;then revision="$SOURCE";fi
 git switch --detach "$revision"
 mkdir "$w/$side/runtime"
 stage "$side-release-copy" 25m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$side/runtime/"
 stage "$side-runtime-manifest" 1m python3 "$support/fileset.py" snapshot "$w/$side/runtime" "$results/$side-runtime-manifest.json"
 python3 "$support/fileset.py" verify "$w/$side/runtime" "$support/prior-$side-runtime.json" "$results/$side-reproduced-prior-runtime.json"
 stage "$side-runtime-archive" 2m tar -C "$w/$side/runtime" -czf "$results/$side-runtime.tar.gz" .
done
stage bazel-shutdown 2m "${bazel_cmd[@]}" shutdown
# All project builds/switches end before BASE/candidate capability visits.
test -z "$(timeout 20s sudo docker ps -aq)"
test ! -L /etc/docker/daemon.json
if sudo test -e /etc/docker/daemon.json;then
 sudo cp -a /etc/docker/daemon.json "$w/docker-original.json";echo present > "$w/docker-config-state"
else echo absent > "$w/docker-config-state";fi
for side in base candidate;do
 test -z "$(timeout 20s sudo docker ps -aq)"
 timeout 30s sudo python3 "$support/quiescent.py" "$active"
 rm -rf -- "$active"
 cp -a "$w/$side/runtime" "$active"
 if test "$side" = base;then
  stage register-runtime 1m sudo "$active/runsc" install --config_file=/etc/docker/daemon.json --runtime=vfio-gofer-pilot --clobber=false --download-sidecars=NEVER --require-sidecars=NEVER -- --platform=systrap --network=sandbox --debug=false --directfs=false
  stage restart-docker 1m sudo systemctl restart docker
 fi
 stage "$side-runtime-ready" 80s sudo python3 "$support/docker_runtime_ready.py" "$results/$side-runtime-readiness.json" /etc/docker/daemon.json "vfio-gofer-pilot=$active/runsc"
 sudo python3 - "$results/$side-runtime-readiness.json" <<'ARGS'
import json
import sys
from pathlib import Path
r=json.loads(Path('/etc/docker/daemon.json').read_text())['runtimes']['vfio-gofer-pilot']
o=json.loads(Path(sys.argv[1]).read_text())['observations'][-1]['taskRuntimes']['vfio-gofer-pilot']
expected=['--platform=systrap','--network=sandbox','--debug=false','--directfs=false']
assert r['runtimeArgs']==expected and o['runtimeArgs']==expected
ARGS
 stage "$side-dropped-credential-access" 2m python3 "$support/runtime_access.py" "$active" "$exec_root" "$results/$side-runtime-access.json"
 # Complete a real configured Docker runtime launch, including the prewarmer,
 # before recording either benchmark observation. No direct invalid invocation.
 stage "$side-configured-runtime-preflight" 2m sudo docker run --rm --name vfio-gofer-preflight --runtime=vfio-gofer-pilot --network=none gvisor.dev/images/benchmarks/alpine:latest /bin/true
 test -z "$(timeout 20s sudo docker ps -aq)"
 timeout 30s sudo python3 "$support/quiescent.py" "$active"
 python3 "$support/fileset.py" verify "$active" "$results/$side-runtime-manifest.json" "$results/$side-runtime-before.json"
 python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$side-harness-before.json"
 sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/rubydev:latest > "$results/$side-images-before.txt"
 cmp "$results/frozen-images.txt" "$results/$side-images-before.txt"
 for suite in startup ruby;do run_suite "$side" vfio-gofer-pilot "$suite";done
 test -z "$(timeout 20s sudo docker ps -aq)"
 timeout 30s sudo python3 "$support/quiescent.py" "$active"
 python3 "$support/fileset.py" verify "$active" "$results/$side-runtime-manifest.json" "$results/$side-runtime-after.json"
 python3 "$support/fileset.py" verify "$w/harness" "$results/frozen-harness.json" "$results/$side-harness-after.json"
 sudo docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/alpine:latest gvisor.dev/images/benchmarks/rubydev:latest > "$results/$side-images-after.txt"
 cmp "$results/frozen-images.txt" "$results/$side-images-after.txt"
done
printf '%s\n' 'AMD64 ordinary-gofer capability/cost only: reused hash-bound native inputs, BASE/candidate dirty-bindfs RubyNoOp1x and StartupEmpty1x, directfs=false. No n24/comparative performance or physical VFIO/TPU/ARM claim.' > "$results/scope.txt"
