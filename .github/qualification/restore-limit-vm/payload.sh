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
[[ "$1" == restore ]]
base="$TEST_SRCDIR/$TEST_WORKSPACE/.github/qualification/restore-limit-vm"
records="$TEST_UNDECLARED_OUTPUTS_DIR"
outer_xml="$XML_OUTPUT_FILE"
mkdir -p /work
cd /work
unset TEST_UNDECLARED_OUTPUTS_DIR RUNTIME GVISOR_SIDECAR_BINARIES_DIR
export TEST_WORKSPACE=_main TEST_TARGET=//runsc/container:container_test
for arm in stock fixed; do
  mkdir "$arm"
  /bin/busybox tar -xf "$base/$arm.tar" -C "$arm"
  sha256sum "$arm/container_test" "$arm/container_test.runfiles/_main/release/runsc" "$arm/container_test.runfiles/_main/release/gvisor-bin/gvisor_sentry" "$base/$arm.tar" | cut -d' ' -f1 > "$records/$arm.sha"
done
# All guest processes execute on the same virtual CPUs in this one boot.
cat /sys/devices/system/cpu/online > "$records/cpus"
sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status > "$records/affinity"
for ((round=0; round<=16; ++round)); do
  arms=(stock fixed)
  if (( round > 0 && round % 2 == 0 )); then arms=(fixed stock); fi
  for arm in "${arms[@]}"; do
    name="r$round-$arm"
    export TEST_SRCDIR="/work/$arm/container_test.runfiles" RUNFILES_DIR="/work/$arm/container_test.runfiles"
    export XML_OUTPUT_FILE="$records/$name.xml" WORKLOAD_CHECKPOINT_DIR="/work/checkpoint"
    mkdir "$WORKLOAD_CHECKPOINT_DIR"
    cd "$TEST_SRCDIR/$TEST_WORKSPACE"
    code=0
    "/work/$arm/container_test" -test.v '-test.run=^TestCheckpointTmpfsTreeForProfile$' > /work/case.log 2>&1 || code=$?
    echo "$code" > "$records/$name.exit"
    if (( code != 0 )); then exit "$code"; fi
    # Only the fixture's public numeric markers reach the reporting process.
    sed -n 's/.*container_save_restore_test.go:[0-9]*: \(TMPFS-.*\)$/\1/p' /work/case.log > "$records/$name.markers"
    for file in checkpoint.img pages.img pages_meta.img; do
      stat -c '%s' "$WORKLOAD_CHECKPOINT_DIR/$file"
    done > "$records/$name.bytes"
    cp "$XML_OUTPUT_FILE" "$outer_xml"
    rm -r "$WORKLOAD_CHECKPOINT_DIR"
  done
done
