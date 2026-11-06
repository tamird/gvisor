#!/bin/bash
# systemd provisions one transient service; the runner user owns the delegated leaf.
set -Eeuo pipefail
phase=$1
trap 'printf "HOST_SETUP_LOCATION=%s:%d\n" "$phase" "$LINENO" >&2' ERR
case "$1" in
  prepare)
    root=$2
    pkg=${0%/*}
    mkdir -p "$root"/{dev,proc,sys,usr,tmp,run,inputs,outputs,work,etc}
    chmod 1777 "$root/tmp"
    for dir in bin lib lib64 sbin; do
        [[ -L /$dir ]]
        ln -s "$(readlink "/$dir")" "$root/$dir"
    done
    printf 'root:x:0:0:root:/work:/bin/bash\n' > "$root/etc/passwd"
    printf 'root:x:0:\n' > "$root/etc/group"
    printf '127.0.0.1 localhost\n' > "$root/etc/hosts"
    cp "$pkg/payload.sh" "$root/inputs/"
    case "$3" in
        guard) touch "$root/inputs/guard-only";;
        measure) cp --reflink=auto "$pkg"/{parent,reference,candidate}.tar "$root/inputs/";;
        *) exit 2;;
    esac
    ;;
  service)
    root=$2
    uid=$(id -u)
    [[ $uid != 0 ]]
    # Only systemd provisions the named service. The controller is delegated
    # to the existing ordinary runner user; the test starts before unshare as that user.
    exec timeout --kill-after=20 7800 sudo -n systemd-run --wait --pipe --collect --quiet \
        --uid="$uid" --gid="$(id -g)" --unit=gvisor-restore-one-cpu.service \
        --property="Delegate=cpu memory" --property=RuntimeMaxSec=7700s \
        /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
        /bin/bash "$0" outer "$root"
    ;;
  outer)
    membership=$(awk -F: '$1=="0" {print $3}' /proc/self/cgroup)
    [[ ${membership##*/} == gvisor-restore-one-cpu.service && $(id -u) != 0 && $(nproc) == 4 ]]
    [[ $(findmnt -nr -o FSROOT,FSTYPE /sys/fs/cgroup) == "/ cgroup2" ]]
    dir=/sys/fs/cgroup$membership
    while :; do
        if [[ -f $dir/cpu.max ]]; then
            read -r quota period < "$dir/cpu.max"
            [[ $quota == max && $period =~ ^[1-9][0-9]*$ ]]
            echo "CPU_MAX=$quota $period"
        fi
        if [[ -f $dir/memory.max ]]; then
            read -r memory < "$dir/memory.max"
            [[ $memory == max || $memory -ge 2147483648 ]]
        fi
        [[ $dir != /sys/fs/cgroup ]] || break
        dir=${dir%/*}
    done
    exec setpriv --no-new-privs unshare --user --map-root-user --mount --cgroup --net --ipc --uts \
        --propagation private /bin/bash "$0" namespace "$2" "$(id -u)" \
        "$(readlink /proc/self/ns/user)" "$(readlink /proc/self/ns/net)" "$(readlink /proc/self/ns/cgroup)" "$(readlink /proc/self/ns/pid)"
    ;;
  namespace)
    root=$2
    [[ $(awk 'NR==1{print $2,$3}' /proc/self/uid_map) == "$3 1" && $(id -u) == 0 ]]
    [[ $(readlink /proc/self/ns/user) != "$4" && $(readlink /proc/self/ns/net) != "$5" && $(readlink /proc/self/ns/cgroup) != "$6" ]]
    [[ $(cat /proc/self/cgroup) == '0::/' ]]
    mount --bind "$root" "$root"
    mount --bind /usr "$root/usr"
    mount -o remount,bind,ro "$root/usr"
    mount -t sysfs -o ro sysfs "$root/sys"
    mount -t cgroup2 none "$root/sys/fs/cgroup"
    cg=$root/sys/fs/cgroup
    [[ $(stat -fc %T "$cg") == cgroup2fs ]]
    mkdir "$cg/client" "$cg/benchmark"
    echo $$ > "$cg/client/cgroup.procs"
    echo "+cpu +memory" > "$cg/cgroup.subtree_control"
    echo "100000 100000" > "$cg/benchmark/cpu.max"
    grep -Fxq "100000 100000" "$cg/benchmark/cpu.max"
    echo 2147483648 > "$cg/benchmark/memory.max"
    grep -Fxq 2147483648 "$cg/benchmark/memory.max"
    mount --bind "$root/inputs" "$root/inputs"
    mount -o remount,bind,ro "$root/inputs"
    for d in null zero full random urandom; do
        touch "$root/dev/$d"
        mount --bind "/dev/$d" "$root/dev/$d"
    done
    ln -s /proc/self/fd "$root/dev/fd"
    ln -s /proc/self/fd/2 "$root/dev/stderr"
    exec unshare --pid --fork --kill-child /bin/bash "$0" pivot "$root" "$7"
    ;;
  pivot)
    root=$2
    mount -t proc proc "$root/proc"
    [[ $(readlink "$root/proc/self/ns/pid") != "$3" ]]
    cd "$root"
    pivot_root . .
    exec /bin/bash -eu -c 'umount -l .; cd /work; [[ ! -e /var/run/docker.sock ]]; exec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C HOME=/work TMPDIR=/tmp OUTER_USER_NS="$(readlink /proc/self/ns/user)" /usr/bin/unshare --user --map-root-user --mount --propagation private /bin/bash /inputs/payload.sh'
    ;;
esac
