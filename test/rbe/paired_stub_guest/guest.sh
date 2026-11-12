#!/bin/bash
set -euo pipefail
inputs="$TEST_SRCDIR/$TEST_WORKSPACE/test/rbe/paired_stub_guest/inputs"
work="$TEST_TMPDIR/paired_stub"
mkdir -p "$work/bundle/rootfs" "$work/state"
cp -RL "$inputs/." "$work/"
cd "$work"
sha256sum -c manifest.sha256 >/dev/null
manifest="$(sha256sum manifest.sha256 | cut -d' ' -f1)"
kernel="$(uname -r)"
pages="$(getconf PAGESIZE)"
arch="$(uname -m)"
printf 'PAIRED_INPUTS manifest=%s arch=%s kernel=%s pages=%s\n' "$manifest" "$arch" "$kernel" "$pages"
case "$kernel/$pages/$arch" in
  7.3.0-070300rc3-generic/4096/aarch64|6.8.0-138-generic-64k/65536/aarch64) ;;
  *) exit 1 ;;
esac
chmod 755 release/runsc release/gvisor-bin/* probe
cp probe bundle/rootfs/probe
cat > bundle/config.json <<'JSON'
{"ociVersion":"1.0.2","root":{"path":"rootfs","readonly":true},"process":{"args":["/probe"],"cwd":"/","user":{"uid":0,"gid":0},"env":["PATH=/"],"noNewPrivileges":true},"linux":{"namespaces":[{"type":"pid"},{"type":"network"},{"type":"ipc"},{"type":"uts"},{"type":"mount"}]}}
JSON
set +e
"$work/release/runsc" --root="$work/state" --platform=systrap --network=none --ignore-cgroups=true \
  --debug-log="$work/debug.log" run --bundle="$work/bundle" paired-stub >stdout 2>stderr
status=$?
set -e
printf 'PAIRED_EXIT status=%s\n' "$status"
if [[ "$status" != 0 ]]; then
  grep -Ehi 'error|fail|panic|fatal|unsupported|not found|permission denied' stderr debug.log | tail -6 | cut -c1-420 || true
  exit 1
fi
selected="$(sed -n 's/^.*Sidecar "gvisor_sentry" found: booting sandbox with //p' debug.log)"
[[ -f "$selected" && "$selected" -ef "$work/release/gvisor-bin/gvisor_sentry" ]]
test "$(cat stdout)" = "SYSTRAP_PROBE arch=aarch64 auxv=$pages sysconf=$pages pid=1"
cat stdout
printf 'PAIRED_SENTRY sha256=%s\n' "$(sha256sum release/gvisor-bin/gvisor_sentry | cut -d' ' -f1)"
