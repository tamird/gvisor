#!/bin/bash
# Run the same two capsules inside the ordinary-user offline namespace.
set -Eeuo pipefail
trap 'printf "PAYLOAD_LOCATION=%d\n" "$LINENO" >&2' ERR
[[ $(readlink /proc/self/ns/user) != "$OUTER_USER_NS" && $(awk 'NR==1{print $1,$2,$3}' /proc/self/uid_map) == "0 0 1" ]]
for mountpoint in /usr /inputs; do
    [[ ,$(findmnt -nr -o VFS-OPTIONS "$mountpoint"), == *,ro,* ]]
done
cg=/sys/fs/cgroup
[[ $(cat /proc/self/cgroup) == '0::/client' && $(cat "$cg/benchmark/memory.max") == 2147483648 ]]
[[ $(getconf PAGESIZE) == 4096 && $(nproc) == 4 ]]
grep -Fxq 'oom_kill 0' "$cg/benchmark/memory.events"
out=/outputs
source_dir=/inputs
for arm in parent candidate; do
    mkdir "/work/$arm"
    sha256sum "$source_dir/$arm.tar" | cut -d' ' -f1 > "$out/$arm-capsule"
    tar --no-same-owner --no-same-permissions -xf "$source_dir/$arm.tar" -C "/work/$arm"
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
printf '<testsuites><testsuite tests="1"><testcase classname="native" name="matchedSave"/></testsuite></testsuites>\n' > /outputs/summary.xml
