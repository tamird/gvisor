#!/bin/bash
set -euo pipefail
# init.sh mounts cgroup2 on this guest's own kernel; no v1 run_under is used.
cg=/sys/fs/cgroup
[[ "$(stat -fc %T "$cg")" == cgroup2fs && "$(getconf PAGESIZE)" == 4096 ]]
/bin/busybox tar --help > /dev/null 2>&1
grep -qw memory "$cg/cgroup.controllers"
echo +memory > "$cg/cgroup.subtree_control"
mkdir "$cg/restore-review"
echo 5368709120 > "$cg/restore-review/memory.max"
echo +memory > "$cg/restore-review/cgroup.subtree_control"
mkdir "$cg/restore-review/client" "$cg/restore-review/probe"
echo $$ > "$cg/restore-review/client/cgroup.procs"
[[ "$(cat /proc/self/cgroup)" == 0::/restore-review/client ]]
[[ "$(cat "$cg/restore-review/memory.max")" == 5368709120 ]]
echo 209715200 > "$cg/restore-review/probe/memory.max"
[[ "$(cat "$cg/restore-review/probe/memory.max")" == 209715200 ]]
grep -Fxq 'oom_kill 0' "$cg/restore-review/probe/memory.events"
rmdir "$cg/restore-review/probe"
printf 'GVISOR_RESTORE_DELEGATED_MEMORY=5368709120\n'
if [[ "$1" == preflight ]]; then
  printf '<testsuites><testsuite tests="1"><testcase classname="cgroup2" name="delegated"/></testsuite></testsuites>\n' > "$XML_OUTPUT_FILE"
  exit 0
fi
[[ "$1" == restore ]]
capsule="$TEST_SRCDIR/$TEST_WORKSPACE/.github/qualification/restore-limit-vm/capsule.tar"
sha256sum "$capsule" | cut -d' ' -f1 > "$TEST_UNDECLARED_OUTPUTS_DIR/capsule-sha256"
mkdir /work/original
/bin/busybox tar -xf "$capsule" -C /work/original
sha256sum /work/original/container_test | cut -d' ' -f1 > "$TEST_UNDECLARED_OUTPUTS_DIR/owner-elf-sha256"
export TEST_SRCDIR=/work/original/container_test.runfiles TEST_WORKSPACE=_main TEST_TARGET=//runsc/container:container_test
export RUNFILES_DIR="$TEST_SRCDIR"
unset TEST_UNDECLARED_OUTPUTS_DIR
cd "$TEST_SRCDIR/$TEST_WORKSPACE"
exec /work/original/container_test -test.v '-test.run=^TestRestoreCgroupMemoryLimit$'
