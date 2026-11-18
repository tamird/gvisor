#!/bin/bash
# Exact-source syscall consumer gate; root/peer review before publication/launch.
set -euo pipefail
if test "${SEMAPHORE_CONSUMER_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=180s 107m env SEMAPHORE_CONSUMER_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 09b196bf630b2a6be83ab28e6c7611f7e766a023
test "$BASE" = c4428378b2cb54c3c94a5c36aaedcbbcc3447513
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/semaphore-syscall-consumer"
results="$RESULT_DIR/semaphore-syscall-consumer"
mkdir -m 700 "$w" "$results"
exec_root=$(mktemp -d /tmp/gvisor-semaphore-consumer.XXXXXX)
chmod 711 "$exec_root"
active="$exec_root/active"
mkdir -m 755 "$active"
stage_pid=
stage_launching=0
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 if test "$stage_launching" = 1 && test -z "$stage_pid";then
  printf '%s\n' 'Stage launch interrupted before PID publication; no competing cleanup' > "$results/cleanup-incomplete.txt"
  printf '%s\n' 1 > "$results/final-status.txt"
  exit 1
 fi
 if test -n "$stage_pid";then
  kill -TERM "$stage_pid" 2>/dev/null || true
  if ! timeout --kill-after=1s 19s python3 - "$stage_pid" <<'WAIT'
import sys,time
from pathlib import Path
pid=int(sys.argv[1]);deadline=time.monotonic()+18
while True:
 try:raw=Path(f'/proc/{pid}/stat').read_text()
 except FileNotFoundError:break
 if raw.rsplit(')',1)[1].split()[0]=='Z':break
 assert time.monotonic()<deadline,'Stage wrapper termination unproved'
 time.sleep(.05)
WAIT
  then printf '%s\n' 'Stage wrapper still active; no competing cleanup' > "$results/cleanup-incomplete.txt";exit 1;fi
  wait "$stage_pid" 2>/dev/null || true
 fi
 if test -e "$results/stage-active-group.json" || test -e "$w/docker-active-group.json";then
  printf '%s\n' 'Owned group quiescence unproved; no competing cleanup' > "$results/cleanup-incomplete.txt"
  exit 1
 fi
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 if timeout --kill-after=1s 5s sudo python3 "$support/quiescent.py" "$active";then
  timeout --kill-after=1s 5s rm -rf -- "$exec_root" || rc=1
 else rc=1;fi
 if test -f "$results/apparmor-original.txt";then
  read -r original < "$results/apparmor-original.txt"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original" > "$results/apparmor-restored.txt" || rc=1
 fi
 if test -f "$results/native-host-prerequisites.json";then
  timeout --kill-after=1s 5s sudo python3 "$support/prerequisites.py" after "$results" || rc=1
 fi
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local label="$1" cap="$2" rc=0 began
 shift 2
 began=$(python3 -c 'import time;print(time.monotonic_ns())')
 printf '%q ' "$@" >> "$results/commands.txt";printf '\n' >> "$results/commands.txt"
 stage_launching=1
 python3 "$support/stage.py" "$label" "$cap" "$results" "$@" &
 stage_pid=$!
 stage_launching=0
 wait "$stage_pid" || rc=$?
 stage_pid=
 timeout --kill-after=1s 5s cat "$results/$label.log" || rc=1
 printf '%s\t%s\t%s\n' "$label" "$(( ($(python3 -c 'import time;print(time.monotonic_ns())')-began)/1000000000 ))" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
# Immutable source and completed first-gate dependency, not revision traversal.
python3 - "$support/expected.json" "$support/prior-source-gate.json" <<'IDENTITY'
import hashlib,json,subprocess,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text())
assert hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()==x['sourceGateEvidenceSHA256']
p=json.loads(Path(sys.argv[2]).read_text());assert p['run']==44322450121 and p['source']==x['source']
for revision in (x['source'],x['base']):subprocess.run(['git','cat-file','-e',revision+'^{tree}'],check=True,timeout=5)
headers=subprocess.check_output(['git','cat-file','commit',x['source']],timeout=5).split(b'\n\n',1)[0].splitlines()
assert [line[7:].decode('ascii') for line in headers if line.startswith(b'parent ')]==[x['base']]
for name,digest in x['sourceDependenciesSHA256'].items():assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest,name
subprocess.run(['git','diff','--quiet'],check=True,timeout=5);subprocess.run(['git','diff','--cached','--quiet'],check=True,timeout=5)
IDENTITY
cp "$support/prior-source-gate.json" "$results/reused-source-gate.json"
uname -a > "$results/worker.txt"
lscpu > "$results/cpu.txt"
sudo -n true
stage native-host-prerequisites 20s sudo python3 "$support/prerequisites.py" before "$results"
stage public-root-preflight 45s python3 "$support/exec_root_probe.py" "$exec_root" "$results/public-root-preflight.json" gvisor-semaphore-consumer.
stage owned-group-interruption-preflight 15s python3 "$support/group_preflight.py" "$w" "$results"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-original.txt"
 stage userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-active.txt"
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 --config=race --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend= --bes_results_url= --color=no --curses=no)
targets=(//test/syscalls/linux:semaphore_test //test/syscalls:semaphore_test_runsc_systrap_directfs //test/syscalls:semaphore_test_runsc_systrap_directfs_save //test/syscalls:semaphore_test_runsc_systrap_directfs_save_resume)
# Resolve actual configured runtime flavor before the cold release build.
stage exact-owner-query 3m "${bazel_cmd[@]}" cquery "${flags[@]}" --output=label "set(${targets[*]}) union labels(runsc,//:release) union labels(sidecars,//:release)"
python3 - "$results/exact-owner-query.log" "$support/expected.json" <<'QUERY'
import json,sys
from pathlib import Path
x=json.loads(Path(sys.argv[2]).read_text())
actual={line.split()[0] for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith('//')}
assert actual==set([x['nativeBinary']]+x['sentryTargets']+x['raceReleaseSelectedTargets']),actual
QUERY
python3 - "$support/expected.json" "$support/prior-race-owner-gate.json" "$results" <<'RACE_REUSE'
import hashlib,json,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text());raw=Path(sys.argv[2]).read_bytes()
assert hashlib.sha256(raw).hexdigest()==x['raceOwnerEvidenceSHA256']
p=json.loads(raw);assert p['run']==44388076421 and p['identity']['source']==x['source']
assert p['outcome']['status']=='passed' and p['actualRaceConfiguration'] and p['GORACE']=='halt_on_error=1'
assert p['caseCount']==8 and p['skips']==p['raceReports']==0 and p['freshUncachedAttempt']
(Path(sys.argv[3])/'reused-race-owner-gate.json').write_bytes(raw)
raw=Path(sys.argv[2]).with_name('prior-race-consumer-gate.json').read_bytes()
assert hashlib.sha256(raw).hexdigest()==x['raceConsumerEvidenceSHA256']
p=json.loads(raw);assert p['run']==44391986188 and p['outcome']['source']==x['source']
assert p['outcome']['status']=='not-passed' and p['ordinary']['freshCases']==30 and p['ordinary']['freshUncachedBEP']
assert p['ordinary']['noFailuresOrSkips'] and p['ordinary']['raceReports']==0 and p['raceRelease']['completeOpaque9Reproduced']
(Path(sys.argv[3])/'reused-race-consumer-gate.json').write_bytes(raw)
RACE_REUSE
# Native30 already passed in44337494099; bind the retained receipt and compiler.
# No dependency installation: normal native release does not export Go sources.
stage native-tool-readiness 30s bash -eu -c '
 test "$(gcc -dumpmachine)" = x86_64-linux-gnu
 for tool in gcc g++ ld objcopy nm;do command -v "$tool"; "$tool" --version;done
 gcc --version > "$1/native-host-gcc-version.txt"
 g++ --version > "$1/native-host-gxx-version.txt"
' readiness "$results"
python3 - "$support/expected.json" "$support/prior-native-gate.json" "$results" <<'NATIVE_REUSE'
import hashlib,json,sys
from pathlib import Path
expected=json.loads(Path(sys.argv[1]).read_text())
raw=Path(sys.argv[2]).read_bytes()
assert hashlib.sha256(raw).hexdigest()==expected['nativeGateEvidenceSHA256'],'Native receipt digest mismatch'
proof=json.loads(raw)
assert proof['run']==44337494099 and proof['identity']['source']==expected['source']
assert proof['identity']['plan']=='7d4119c08fb8efcb74108718a451dcc8ed0cda9f'
assert proof['native']['commandExit']==0 and proof['native']['BEPOverallSuccess']
assert proof['native']['caseCount']==30 and set(proof['native']['cases'])==set(expected['cases'])
assert proof['native']['IPC_after']==0 and proof['outcome']['steps']['qualify']=='failure'
results=Path(sys.argv[3])
for cc,digest in expected['nativeCompilerVersionSHA256'].items():
 assert hashlib.sha256((results/f'native-host-{cc}-version.txt').read_bytes()).hexdigest()==digest,cc+' differs from native control'
(results/'reused-native-gate.json').write_bytes(raw)
NATIVE_REUSE
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}" "RUNTIME_DIR=$active" "RUNTIME_BIN=$active/runsc" RUNSC_TARGET=//runsc STAGED_BINARIES= EXTRA_SIDECAR_TARGETS=)
stage source-release 30m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$active/"
stage runtime-elf-identity 15s python3 "$support/release.py" "$active" "$results/runtime-elf-identity.json"
stage runtime-manifest 30s python3 "$support/fileset.py" snapshot "$active" "$results/runtime-manifest.json"
stage runtime-actor-access 30s python3 "$support/runtime_access.py" "$active" "$exec_root" "$results/runtime-access.json"
stage runtime-archive 1m tar -C "$active" -czf "$results/runtime.tar.gz" .
for phase in save-restore save-resume;do
 case "$phase" in ordinary) index=1;cap=20m;; save-restore) index=2;cap=35m;test_seconds=1800;; save-resume) index=3;cap=25m;test_seconds=1200;; esac
 stage "$phase-runtime-before" 30s python3 "$support/fileset.py" verify "$active" "$results/runtime-manifest.json" "$results/$phase-runtime-before.json"
 python3 -c 'import time;print(time.time_ns()//1000000)' > "$results/$phase-start-ms.txt"
 rc=0
 options="--nocache_test_results --flaky_test_attempts=1 --test_sharding_strategy=disabled --test_timeout=$test_seconds --test_output=all --test_env=GORACE=halt_on_error=1 --test_tag_filters= --build_event_json_file=$results/$phase.bep"
 stage "$phase" "$cap" make "${make_args[@]}" syscall-tests "TARGETS=${targets[$index]}" "OPTIONS=$options" || rc=$?
 printf '%s\n' "$rc" > "$results/$phase-exit.txt"
 stage "$phase-evidence" 1m python3 "$support/collect.py" "$phase" "$results" "$support/expected.json" "$active/runsc"
 test "$rc" = 0
 stage "$phase-runtime-after" 30s python3 "$support/fileset.py" verify "$active" "$results/runtime-manifest.json" "$results/$phase-runtime-after.json"
done
stage final-shutdown 1m "${bazel_cmd[@]}" shutdown
printf '%s\n' 'Reused44337494099 nativeLinux30; reused actual raceordinary30+Go8; fresh race save/restore and save/resume30each; no ARM/performance claim.' > "$results/scope.txt"
