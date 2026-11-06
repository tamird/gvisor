#!/bin/bash
# Run all three capsules inside the ordinary-user offline namespace.
set -Eeuo pipefail
trap 'printf "PAYLOAD_LOCATION=%d\n" "$LINENO" >&2' ERR
if [[ -f /inputs/guard-only ]]; then
    old_rc=0
    awk 'NR==1{print $1,$2,$3}' /proc/self/uid_map > /work/old-map 2>/work/old-error || old_rc=$?
    echo "OLD_AWK_EXIT=$old_rc"
    if [[ -L /usr/bin/awk && $(readlink /usr/bin/awk) == /etc/alternatives/awk && ! -e /etc/alternatives/awk ]]; then
        echo OLD_AWK_BROKEN_ALTERNATIVE
    fi
fi
[[ $(readlink /proc/self/ns/user) != "$OUTER_USER_NS" ]]
read -r inside outside count extra < /proc/self/uid_map
[[ "$inside:$outside:$count:$extra" == "0:0:1:" ]]
echo INNER_UID_MAP_VERIFIED
for mountpoint in /usr /inputs; do
    [[ ,$(findmnt -nr -o VFS-OPTIONS "$mountpoint"), == *,ro,* ]]
done
cg=/sys/fs/cgroup
[[ $(cat /proc/self/cgroup) == '0::/client' && $(cat "$cg/benchmark/memory.max") == 2147483648 ]]
grep -Fxq "100000 100000" "$cg/benchmark/cpu.max"
[[ $(getconf PAGESIZE) == 4096 && $(nproc) == 4 ]]
grep -Fxq 'oom_kill 0' "$cg/benchmark/memory.events"
if [[ -f /inputs/guard-only ]]; then
    [[ ! -e /inputs/parent.tar && ! -e /inputs/candidate.tar ]]
    (
        child=$BASHPID
        echo "$child" > "$cg/benchmark/cgroup.procs"
        [[ $(cat /proc/self/cgroup) == '0::/benchmark' ]]
        grep -Fxq "$child" "$cg/benchmark/cgroup.procs"
    )
    echo PRIVATE_GUARD_PASSED
    exit 0
fi
out=/outputs
source_dir=/inputs
for arm in parent reference candidate; do
    mkdir "/work/$arm"
    sha256sum "$source_dir/$arm.tar" | cut -d' ' -f1 > "$out/$arm-capsule"
    tar --no-same-owner --no-same-permissions -xf "$source_dir/$arm.tar" -C "/work/$arm"
    sha256sum "/work/$arm/container_test" | cut -d' ' -f1 > "$out/$arm-elf"
done
test_one() {
    local sample=$1 arm=$2 code=0
    local prefix=$sample-$arm
    echo "$turn" > "$out/$prefix.sequence"
    turn=$((turn+1))
    local tree=/work/$arm/container_test.runfiles
    (
        export TEST_SRCDIR="$tree" RUNFILES_DIR="$tree" TEST_WORKSPACE=_main TEST_TARGET=//runsc/container:container_test
        export GVISOR_SIDECAR_BINARIES_DIR="$tree/_main/release/gvisor-bin"
        export XML_OUTPUT_FILE="$out/$prefix.xml"
        unset TEST_UNDECLARED_OUTPUTS_DIR
        cd "$tree/_main"
        "/work/$arm/container_test" -test.v '-test.run=^TestGCReferenceRestore$'
    ) > "/work/test.log" 2>&1 || code=$?
    echo "$code" > "$out/$prefix.exit"
    sha256sum /work/test.log | cut -d' ' -f1 > "$out/$prefix.loghash"
    sed -n 's/^.*GC-RESTORE-ROW \({.*}\)$/\1/p' /work/test.log > "$out/$prefix.row"
    [[ $code == 0 ]] || { grep -oE 'gc_reference_test\.go:[0-9]+' /work/test.log | head -4 > "$out/$prefix.locations" || :; return "$code"; }
    [[ $(wc -l < "$out/$prefix.row") == 1 ]]
}
turn=0
orders=('parent reference candidate' 'reference candidate parent' 'candidate parent reference'
        'parent candidate reference' 'candidate reference parent' 'reference parent candidate')
end=36
if [[ -f /inputs/warmup-only ]]; then end=0; fi
for ((i=-1; i<end; i++)); do
    index=0
    if ((i >= 0)); then index=$((i % 6)); fi
    for arm in ${orders[index]}; do test_one "$i" "$arm"; done
done
printf '<testsuites><testsuite tests="1"><testcase classname="native" name="matchedRestore"/></testsuite></testsuites>\n' > /outputs/summary.xml
