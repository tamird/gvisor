#!/bin/bash
# Run each required lint gate independently from the functional screen.
set -euo pipefail
: "${SOURCE:?}" "${RESULT_DIR:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
gate=${1:?source or cpp}
test "$gate" = source || test "$gate" = cpp
test "$SOURCE" = 4f51c3b3a663f39cb1dec661f83406e4f014348a
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
results="$RESULT_DIR/vfio-$gate-lint"
mkdir -m 700 "$results"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 if test -e "$results/stage-active-group.json";then
  printf '%s\n' 'Owned group quiescence unproved; inputs retained' > "$results/cleanup-incomplete.txt"
  exit 1
 fi
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 if test -f "$results/apparmor-original.txt";then
  read -r original < "$results/apparmor-original.txt"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original" > "$results/apparmor-restored.txt" || rc=1
 fi
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-original.txt"
 sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
target=lint; cap=10m
if test "$gate" = cpp;then target=lint-cc; cap=40m;fi
python3 "$support/stage.py" "$gate-lint" "$cap" "$results" make DOCKER_BUILD=false \
 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' \
 'BAZEL_OPTIONS=--config=x86_64 --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend=' "$target"
cat "$results/$gate-lint.log"
