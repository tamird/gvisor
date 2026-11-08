#!/bin/sh
# The guest arm is run by the pinned static BusyBox after pivot_root.
set -eu
if [ "${1:-}" = guest ]; then
    export PATH=/bin HOME=/tmp GO_TEST_WRAP=0 GOMAXPROCS=2
    export DOCKER_HOST=unix:///run/docker.sock
    test ! -e /run/docker.sock
    test ! -e /var/run/docker.sock
    if command -v docker; then exit 1; fi
    /busybox grep -Eq '^NoNewPrivs:[[:space:]]+1$' /proc/self/status

    runsc() {
        /release/runsc --root=/run/state --rootless --platform=systrap --network=none \
            --overlay2=none --host-settings=ignore --ignore-cgroups "$@"
    }
    tree() {
        runsc exec captured /test_app "$1" --target-dir=/tmpfs --depth=2 --file-per-level=4 --file-size=64
    }
    trap 'runsc delete --force captured >/dev/null 2>&1 || :' EXIT
    runsc run --detach --bundle=/bundle captured
    tree fsTreeCreate
    tree fsTreeVerify
    runsc checkpoint --leave-running --compression=none --state-engine=graph --image-path=/media captured
    runsc delete --force captured
    runsc restore --detach --bundle=/bundle --image-path=/media captured
    tree fsTreeVerify
    echo VERIFIED_REAL_RESTORE
    runsc delete --force captured
    trap - EXIT

    echo '{"names":["__no_name_0"],"mounts":["/","/tmpfs"]}' > /media/spec.json
    cd /media
    for f in checkpoint.img pages_meta.img pages.img spec.json; do test -f "$f"; done
    test -s checkpoint.img
    /busybox sha256sum checkpoint.img pages_meta.img pages.img spec.json > /media_before
    /busybox cat /media_before
    cd /
    for exe in /stock /fixed; do
        "$exe" -help > /help 2>&1
        /busybox grep -q '^Usage:' /help
        /busybox grep -q -- '-checkpoint-dir' /help
    done
    echo VERIFIED_HELP_BOTH

    reject_docker() {
        name=$1
        shift
        if "$@" >/case.log 2>&1; then echo "unexpected success: $name"; return 1; fi
        /busybox cat /case.log
        /busybox grep -Fq 'docker version --format {{.Server.Version}}' /case.log
        /busybox grep -Fq 'executable file not found' /case.log
        if /busybox grep -q '^=== RUN ' /case.log; then return 1; fi
        echo "VERIFIED_DOCKER_ERROR $name"
    }
    reject_docker stock-directory /stock -checkpoint-dir=/media -test.v -test.run='^TestCheckpointFileRoundTrip$'
    reject_docker fixed-live /fixed -test.run='^$' -test.bench='^BenchmarkCheckpointFile$/^EncodeStatefile$' -test.benchtime=1x

    /fixed -checkpoint-dir=/media -test.v -test.run='^TestCheckpointFileRoundTrip$' \
        -test.bench='^BenchmarkCheckpointFile$/^EncodeStatefile$' -test.benchtime=1x > /case.log 2>&1 || {
            /busybox cat /case.log
            exit 1
        }
    /busybox cat /case.log
    /busybox grep -q '^--- PASS: TestCheckpointFileRoundTrip ' /case.log
    /busybox grep -Eq 'State file sizes: original [1-9][0-9]*, re-encoded \[[1-9][0-9]* [1-9][0-9]* [1-9][0-9]*\]' /case.log
    /busybox grep -Eq '^BenchmarkCheckpointFile/EncodeStatefile(-[0-9]+)?[[:space:]]+1[[:space:]].*[[:space:]][1-9][0-9]*[[:space:]]+encoded-kernel-B' /case.log
    cd /media
    /busybox sha256sum -c /media_before
    echo VERIFIED_CHECKPOINT_DIRECTORY
    exit 0
fi

root="$TEST_TMPDIR/root"
test "$(id -u)" != 0
grep -Eq '^CapEff:[[:space:]]+0+$' /proc/self/status
mkdir -p "$root"/bin "$root"/proc "$root"/sys "$root"/dev "$root"/tmp "$root"/run \
    "$root"/etc "$root"/media "$root"/bundle/rootfs/tmpfs "$root"/bundle/rootfs/proc
cp -RL release "$root/release"
cp "$1" "$root/fixed"
cp "$2" "$root/bundle/rootfs/test_app"
cp "$3" "$root/stock"
cp "$4" "$root/busybox"
cp "$0" "$root/case.sh"
for exe in "$root/fixed" "$root/stock" "$root/bundle/rootfs/test_app" "$root/release/runsc" "$root/release/gvisor-bin/"*; do
    chmod +x "$exe"
    # Some release sidecars link the system C library.
    if readelf -lW "$exe" | grep -q 'Requesting program interpreter:'; then
        ldd "$exe" > "$TEST_TMPDIR/ldd"
        if grep -q 'not found' "$TEST_TMPDIR/ldd"; then exit 1; fi
        awk '/=> \// {print $3} /^[[:space:]]*\// {print $1}' "$TEST_TMPDIR/ldd" | while read -r lib; do
            cp -L --parents "$lib" "$root"
        done
    fi
done
sha256sum "$1" "$2" "$3" release/runsc release/gvisor-bin/gvisor_sentry
cmp "$1" "$root/fixed"
cmp "$2" "$root/bundle/rootfs/test_app"
cmp "$3" "$root/stock"
cmp release/runsc "$root/release/runsc"
cmp release/gvisor-bin/gvisor_sentry "$root/release/gvisor-bin/gvisor_sentry"
if cmp -s "$1" "$3"; then exit 1; fi
chmod 755 "$root/busybox"
ln -s /busybox "$root/bin/sh"
cat > "$root/bundle/config.json" <<'SPEC'
{"ociVersion":"1.0.2","root":{"path":"rootfs"},"process":{"terminal":false,"user":{"uid":0,"gid":0},"cwd":"/","args":["/test_app","reaper"],"env":["PATH=/","GOMAXPROCS=2"]},"mounts":[{"type":"proc","source":"proc","destination":"/proc"},{"type":"tmpfs","source":"tmpfs","destination":"/tmpfs"}],"linux":{"namespaces":[{"type":"pid"},{"type":"mount"},{"type":"ipc"},{"type":"uts"},{"type":"network"}]}}
SPEC
# This is the exact prior namespace initializer with only its post-pivot command selected.
exec env -i PATH=/usr/bin:/bin LANG=C HOME=/tmp /usr/bin/setpriv --no-new-privs /usr/bin/unshare \
    --user --map-root-user --mount --net --pid --fork --kill-child --ipc --uts --propagation private \
    /bin/sh "$5" "$root" "$(id -u)" "$(readlink /proc/self/ns/user)" "$(readlink /proc/self/ns/net)" \
    "$(readlink /proc/self/ns/pid)" /usr/sbin/pivot_root
