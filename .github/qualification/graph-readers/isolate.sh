#!/bin/bash
# Run the existing measurement driver inside the same ordinary-UID user
# namespace as every runsc command, on disk-backed checkpoint files.
set -euo pipefail
capsule=$1
source_rev=$2
base_rev=$3
outer_uid=$(id -u)
test "$outer_uid" -ne 0
mkdir -p "$capsule"/{dev,proc,sys,usr,tmp,run}
chmod 1777 "$capsule/tmp"
for dir in bin lib lib64 sbin; do
    test -L "/$dir"
    ln -s "$(readlink "/$dir")" "$capsule/$dir"
done
exec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
    /usr/bin/setpriv --no-new-privs /usr/bin/unshare --user --map-root-user \
    --mount --net --pid --fork --kill-child --ipc --uts --propagation private \
    /bin/bash -eu -c '
        root=$1
        test "$(awk "NR==1{print \$2,\$3}" /proc/self/uid_map)" = "$2 1"
        test "$(readlink /proc/self/ns/user)" != "$3"
        test "$(readlink /proc/self/ns/net)" != "$4"
        test "$(readlink /proc/self/ns/pid)" != "$5"
        mount --bind "$root" "$root"
        mount --bind /usr "$root/usr"
        mount -o remount,bind,ro "$root/usr"
        mount -t proc proc "$root/proc"
        mount -t sysfs -o ro sysfs "$root/sys"
        for dev in null zero full random urandom; do
            touch "$root/dev/$dev"
            mount --bind "/dev/$dev" "$root/dev/$dev"
        done
        ln -s /proc/self/fd "$root/dev/fd"
        ln -s /proc/self/fd/2 "$root/dev/stderr"
        cd "$root"
        pivot_root . .
        exec /bin/bash -eu -c '\''umount -l .
            cd /work
            test ! -e /var/run/docker.sock
            exec env -i HOME=/work TMPDIR=/tmp PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 \
                RUNNER_TEMP=/work OUTER_UID="$1" SOURCE="$2" BASE="$3" \
                /usr/bin/python3 -B /work/measure.py'\'' graph "$2" "$6" "$7"
    ' graph "$capsule" "$outer_uid" "$(readlink /proc/self/ns/user)" \
      "$(readlink /proc/self/ns/net)" "$(readlink /proc/self/ns/pid)" "$source_rev" "$base_rev"
