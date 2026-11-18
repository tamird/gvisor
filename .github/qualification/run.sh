#!/bin/bash
# Cheap native ARM host/label preflight; no project build or workload execution.
set -euo pipefail
if test "${SEM_ARM_PREFLIGHT_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=60s 240s env SEM_ARM_PREFLIGHT_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 09b196bf630b2a6be83ab28e6c7611f7e766a023
test "$BASE" = c4428378b2cb54c3c94a5c36aaedcbbcc3447513
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = arm64:aarch64:aarch64
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
results="$RESULT_DIR/semaphore-arm-availability"
w="$RUNNER_TEMP/semaphore-arm-availability"
mkdir -m 700 "$results" "$w"
public=
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 if test -e "$w/preflight-active-group.json"; then
  printf '%s\n' 'Owned preflight group quiescence unproved; preserve task directories' > "$results/cleanup-incomplete.txt"
  printf '%s\n' 1 > "$results/final-status.txt"
  exit 1
 fi
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown > "$results/bazel-shutdown.txt" 2>&1 || rc=1
 if test -n "$public"; then
  timeout --kill-after=1s 3s rm -f -- "$public/public-host-execution-probe" || rc=1
  timeout --kill-after=1s 3s rmdir -- "$public" || rc=1
 fi
 if test -f "$results/native-host-prerequisites.json"; then
  timeout --kill-after=1s 5s sudo -n python3 "$support/prerequisites.py" after "$results" || rc=1
 fi
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
phase() {
 local name="$1" cap="$2" rc=0
 shift 2
 printf '%q ' "$@" >> "$results/commands.txt"; printf '\n' >> "$results/commands.txt"
 timeout --signal=TERM --kill-after=15s "$cap" "$@" > "$results/$name.log" 2>&1 || rc=$?
 printf '%s\t%s\n' "$name" "$rc" >> "$results/stages.tsv"
 cat "$results/$name.log"
 return "$rc"
}
phase source-identity 10s python3 - "$SOURCE" "$BASE" <<'PY'
import subprocess,sys
source,base=sys.argv[1:]
assert subprocess.check_output(['git','rev-parse','HEAD'],timeout=3).decode().strip()==source
for revision in (source,base):subprocess.run(['git','cat-file','-e',revision+'^{tree}'],check=True,timeout=3)
headers=subprocess.check_output(['git','cat-file','commit',source],timeout=3).split(b'\n\n',1)[0].splitlines()
assert [v[7:].decode() for v in headers if v.startswith(b'parent ')]==[base]
PY
phase native-host 15s bash -eu -c '
 uname -a
 id
 sudo -n true
 test "$(gcc -dumpmachine)" = aarch64-linux-gnu
 for tool in gcc g++ ld objcopy nm; do command -v "$tool"; "$tool" --version; done
 python3 -c '\''import pathlib,struct; b=pathlib.Path("/usr/bin/true").resolve().read_bytes();assert b[:4]==b"\x7fELF" and struct.unpack("<H",b[18:20])[0]==183; print("native true ELF e_machine=183")'\''
'
phase docker-readiness 15s bash -eu -c '
 docker version --format "{{json .Server}}"
 docker info --format "{{json .}}" > "$1/docker-info.json"
 python3 - "$1/docker-info.json" <<'\''PY'\''
import json,sys
x=json.load(open(sys.argv[1])); assert x["Architecture"] in ("aarch64","arm64"),x["Architecture"]
assert "runc" in x["Runtimes"],x["Runtimes"]
assert x["ContainersRunning"]==0, "Exclusive worker has unrelated running containers"
print("Native ARM Docker server/runc runtime metadata available; no Sentry runtime installed")
PY
' docker "$results"
phase ipc-capabilities 10s sudo -n python3 "$support/prerequisites.py" before "$results"
phase native-ipc-namespace 5s sudo -n unshare --ipc /usr/bin/true
public=$(mktemp -d /tmp/gvisor-sem-arm-preflight.XXXXXX)
chmod 711 "$public"
phase dropped-actor-execution 35s python3 "$support/exec_root_probe.py" "$public" "$results/public-root.json" gvisor-sem-arm-preflight.
phase owned-group-interruption 15s python3 "$support/group_preflight.py" "$w" "$results"
phase canonical-bazel-version 10s bash -eu -c 'test "$(bazel --version)" = "bazel 9.3.1"; bazel --version'
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
phase canonical-bazel-workspace 20s "${bazel_cmd[@]}" info --config=aarch64 --remote_cache= --remote_executor= --bes_backend= --bes_results_url= workspace
labels=(//pkg/sentry/kernel/semaphore:semaphore_test //test/syscalls/linux:semaphore_test //test/syscalls:semaphore_test_runsc_systrap_directfs //test/syscalls:semaphore_test_runsc_systrap_directfs_save //test/syscalls:semaphore_test_runsc_systrap_directfs_save_resume)
phase canonical-owner-query 75s "${bazel_cmd[@]}" query --remote_cache= --remote_executor= --bes_backend= --bes_results_url= --output=label "set(${labels[*]})"
phase exact-query-output 5s python3 - "$results/canonical-owner-query.log" "${labels[@]}" <<'PY'
import pathlib,sys
lines=pathlib.Path(sys.argv[1]).read_text().splitlines();actual=[v for v in lines if v.startswith('//')]
assert len(actual)==len(set(actual))==5 and set(actual)==set(sys.argv[2:]),actual
PY
printf '%s\n' 'Native ARM host/privilege/IPC/group/public-path and canonical Bazel label availability only. No project compile/test, Sentry runtime execution, restore, race, C++ lint or performance qualification.' > "$results/scope.txt"
