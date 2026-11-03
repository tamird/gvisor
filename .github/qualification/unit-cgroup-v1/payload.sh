#!/bin/bash
# Fork-only adapter for the unchanged native test executables and runfiles.
set -euo pipefail
mode=$1
name=$2
pkg=$TEST_SRCDIR/$TEST_WORKSPACE/.github/qualification/unit-cgroup-v1
mapfile -t expected < "$pkg/$name.inputs"
archive=$pkg/$name.tar
actual=$(sha256sum "$archive" | cut -d' ' -f1)
[[ $actual == "${expected[0]}" ]]
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
test -r /dev/kvm && test -w /dev/kvm
if command -v docker || command -v dockerd; then
  exit 1
fi
test ! -S /var/run/docker.sock
if [[ $mode == v1 ]]; then
  test "$(stat -f -c %T /sys/fs/cgroup)" = tmpfs
  awk '$0 ~ / - cgroup / { n++ } END { exit n != 13 }' /proc/self/mountinfo
  export CGROUPV2=false
else
  test "$mode" = v2
  test "$(stat -f -c %T /sys/fs/cgroup)" = cgroup2fs
  export CGROUPV2=true
fi
mkdir -p /work/original /work/erofs /work/bin
/bin/busybox tar -xf "$archive" -C /work/original
/bin/busybox tar -xf "$pkg/erofs.tar" -C /work/erofs
binary=/work/original/${expected[1]}
runfiles=/work/original/${expected[2]}
test "$(sha256sum "$binary" | cut -d' ' -f1)" = "${expected[3]}"
if [[ ${expected[4]} != - ]]; then
  test "$(sha256sum "$runfiles/_main/release/runsc" | cut -d' ' -f1)" = "${expected[4]}"
fi
# Keep the newer tool's libc isolated from the Jammy guest. This is the
# existing container-test tool archive's own interpreter and library closure.
loader=/work/erofs/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
[[ -f $loader ]] || loader=/work/erofs/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
test -x "$loader"
test -x /work/erofs/usr/bin/mkfs.erofs
cat > /work/bin/mkfs.erofs <<EOF
#!/bin/bash
exec "$loader" --inhibit-cache --library-path /work/erofs/usr/lib/x86_64-linux-gnu:/work/erofs/lib/x86_64-linux-gnu /work/erofs/usr/bin/mkfs.erofs "\$@"
EOF
chmod 755 /work/bin/mkfs.erofs
ln -s /bin/busybox /work/bin/tar
export PATH=/work/bin:$PATH
mkfs.erofs -V > /work/erofs-version 2>&1
printf '%s\n' "$mode" "$actual" "${expected[3]}" docker_absent kvm_present \
  "CGROUPV2=$CGROUPV2" "$(uname -r)" "$(sha256sum /work/erofs/usr/bin/mkfs.erofs | cut -d' ' -f1)" \
  "$(sha256sum "$loader" | cut -d' ' -f1)" "${TEST_SHARD_INDEX:-0}" "${TEST_TOTAL_SHARDS:-1}" \
  "$(sha256sum "$pkg/erofs.tar" | cut -d' ' -f1)" > "$TEST_UNDECLARED_OUTPUTS_DIR/proof"
export TEST_SRCDIR=$runfiles RUNFILES_DIR=$runfiles TEST_WORKSPACE=_main
export TEST_TARGET=//runsc/container:$name TEST_BINARY=${expected[1]}
export GO_TEST_RUN_FROM_BAZEL=1 GO_TEST_WRAP_TESTV=true RUNTIME=buildkite_runtime
cd "$runfiles/_main"
if [[ $name == container_test || $name == boot_test ]]; then
  exec "$binary" --test_platforms=ptrace,kvm,systrap
fi
exec "$binary"
