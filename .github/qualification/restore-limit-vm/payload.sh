#!/bin/bash
# The author binaries run only in this offline, hardware-accelerated guest.
set -euo pipefail
cg=/sys/fs/cgroup
[[ $(stat -fc %T "$cg") == cgroup2fs && $(getconf PAGESIZE) == 4096 ]]
[[ $(getconf _NPROCESSORS_ONLN) == 4 && $(id -u) == 0 ]]
for controller in cpu cpuset io memory pids; do
    grep -qw "$controller" "$cg/cgroup.controllers"
    echo "+$controller" > "$cg/cgroup.subtree_control"
done
mkdir "$cg/preflight"
echo 2147483648 > "$cg/preflight/memory.max"
[[ $(cat "$cg/preflight/memory.max") == 2147483648 && $(cat "$cg/preflight/cpu.max") == "max "* ]]
grep -Fxq 'oom_kill 0' "$cg/preflight/memory.events"
rmdir "$cg/preflight"
echo GC_SAVE_VALIDATED_MEMORY=2147483648
if [[ $1 == preflight ]]; then
    printf '<testsuites><testsuite tests="1"><testcase classname="cgroup2" name="delegated"/></testsuite></testsuites>\n' > "$XML_OUTPUT_FILE"
    exit 0
fi
[[ $1 == restore ]]
out=$TEST_UNDECLARED_OUTPUTS_DIR
source_dir=$TEST_SRCDIR/$TEST_WORKSPACE/.github/qualification/restore-limit-vm
for arm in parent candidate; do
    mkdir "/work/$arm"
    sha256sum "$source_dir/$arm.tar" | cut -d' ' -f1 > "$out/$arm-capsule"
    /bin/busybox tar -xf "$source_dir/$arm.tar" -C "/work/$arm"
    sha256sum "/work/$arm/container_test" | cut -d' ' -f1 > "$out/$arm-elf"
done
test_one() {
    local sample=$1 arm=$2 code=0
    local prefix=$sample-$arm
    local tree=/work/$arm/container_test.runfiles
    (
        export TEST_SRCDIR="$tree" RUNFILES_DIR="$tree" TEST_WORKSPACE=_main TEST_TARGET=//runsc/container:container_test
        export XML_OUTPUT_FILE="$out/$prefix.xml"
        unset TEST_UNDECLARED_OUTPUTS_DIR
        cd "$tree/_main"
        "/work/$arm/container_test" -test.v '-test.run=^TestGCReferenceSave$'
    ) > "/work/test.log" 2>&1 || code=$?
    echo "$code" > "$out/$prefix.exit"
    sha256sum /work/test.log | cut -d' ' -f1 > "$out/$prefix.loghash"
    sed -n 's/^.*GC-SAVE-ROW \({.*}\)$/\1/p' /work/test.log > "$out/$prefix.row"
    [[ $code == 0 ]] || { grep -oE 'gc_reference_test\.go:[0-9]+' /work/test.log | head -4 > "$out/$prefix.locations" || :; return "$code"; }
    [[ $(wc -l < "$out/$prefix.row") == 1 ]]
}
for ((i=-1; i<12; i++)); do
    if ((i >= 0 && i % 2 == 1)); then order=(candidate parent); else order=(parent candidate); fi
    for arm in "${order[@]}"; do test_one "$i" "$arm"; done
done
printf '<testsuites><testsuite tests="1"><testcase classname="native" name="matchedSave"/></testsuite></testsuites>\n' > "$XML_OUTPUT_FILE"
