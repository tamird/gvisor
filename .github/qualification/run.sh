#!/bin/bash
set -euo pipefail
# Exact proposal source; baseline control exits after native execution.
candidate=ab45538513c30723c6fe2cdcfc36d1a6fd96f543
base=1f33fe3003455db178b97f3b43d3a012f1611994
test "$SOURCE" = "$base"
test "$(git rev-parse HEAD)" = "$SOURCE"
case "$QUAL_ARCH:$QUAL_CONFIG" in amd64:x86_64|arm64:aarch64) ;; *) exit 2 ;; esac
mkdir -p "$RESULT_DIR"
uname -a > "$RESULT_DIR/worker.txt"
for file in /proc/sys/kernel/io_uring_disabled /proc/self/status; do
  if test -r "$file"; then cat "$file" >> "$RESULT_DIR/host-capabilities.txt"; fi
done
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
status=0
flags=("--config=$QUAL_CONFIG" --jobs=3 --local_test_jobs=2 --strip=never --nostamp --remote_executor= --remote_cache= --bes_backend=)
args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
trap '"${bazel_cmd[@]}" shutdown || true' EXIT
run() {
  local name="$1" rc=0
  shift
  "$@" 2>&1 | tee "$RESULT_DIR/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$SOURCE" "$name" "$rc" >> "$RESULT_DIR/commands.tsv"
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
capture() {
  local label="$1" phase="$2" logs
  logs="$("${bazel_cmd[@]}" info bazel-testlogs)"
  python3 - "$logs/test/syscalls/$label" "$RESULT_DIR/$phase" <<'PY'
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

source, output = map(Path, sys.argv[1:])
output.mkdir(parents=True, exist_ok=True)
for name in ('test.xml', 'test.log'):
    shutil.copy2(source / name, output / name)
cases = ET.parse(output / 'test.xml').getroot().findall('.//testcase')
failures = [case.attrib for case in cases if any(case.find(tag) is not None for tag in ('failure', 'error'))]
skips = [dict(case.attrib, reason=case.find('skipped').text) for case in cases if case.find('skipped') is not None]
(output / 'case-report.json').write_text(json.dumps({'cases': len(cases), 'failures': failures, 'skips': skips}, indent=2) + '\n')
assert cases and not failures and not skips, (len(cases), failures, skips)
PY
}
opts='--nocache_test_results --test_output=errors --test_sharding_strategy=disabled --flaky_test_attempts=1'
native_ok=false
if run native make "${args[@]}" syscall-tests TARGETS=//test/syscalls:iouring_test_native "OPTIONS=$opts"; then
  if capture iouring_test_native native-results; then native_ok=true; else status=1; fi
else
  capture iouring_test_native native-results || true
fi
if test "$SOURCE" = "$base"; then exit "$status"; fi
# A native failure or capability skip is evidence to diagnose, never a pass.
if "$native_ok"; then
  for variant in systrap_directfs systrap_shared; do
    label="iouring_test_runsc_$variant"
    run "$variant" make "${args[@]}" syscall-tests "TARGETS=//test/syscalls:$label" "OPTIONS=$opts" || true
    capture "$label" "$variant-results" || status=1
  done
else
  printf '%s\n' 'Sentry execution withheld because native io_uring did not pass without skips.' > "$RESULT_DIR/sentry-not-run.txt"
fi
run build make "${args[@]}" build TARGETS='//test/util:io_uring_util //test/syscalls/linux:iouring_test' || true
run lint make "${args[@]}" lint || true
run lint-cc make "${args[@]}" lint-cc || true
exit "$status"
