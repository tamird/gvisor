#!/bin/bash
set -euo pipefail
: "${SOURCE:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}"
test "$(git rev-parse HEAD)" = "$SOURCE"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
w="$RUNNER_TEMP/qualification/syscall-native"
mkdir -p "$w/runtime"
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=("--config=$QUAL_CONFIG" --jobs=3 --strip=never --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
# Validation covers the native ELF test entrypoint and actual syscall cases.
# It does not claim all transitive Bazel actions can execute without a shell.
# Stage the canonical runtime bundle; these are ordinary binaries, no replacement.
make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/runtime/" 2>&1 | tee "$RESULT_DIR/runtime.log"
"${bazel_cmd[@]}" test "${flags[@]}" //test/runner:runner_test --nocache_test_results --test_output=errors 2>&1 | tee "$RESULT_DIR/runner-owner.log"
for group in uname atime random; do
  case "$group" in
    uname) owner=uname_test; filter='*' ;;
    atime) owner=stat_times_test; filter=StatTimesTest.FileRead ;;
    random) owner=epoll_test; filter=EpollTest.WatchOpenRandomDevice ;;
  esac
  targets=("//test/syscalls:${owner}_native" "//test/syscalls:${owner}_runsc_systrap_directfs" "//test/syscalls:${owner}_runsc_systrap_shared")
  "${bazel_cmd[@]}" build "${flags[@]}" "${targets[@]}" 2>&1 | tee "$RESULT_DIR/$group-build.log"
  # Each selected test entry must resolve to native ELF, never the removed Bash
  # launcher. This complements native_test's direct symlink implementation.
  for target in "${targets[@]}"; do
    binary="$("${bazel_cmd[@]}" cquery "${flags[@]}" --output=files "$target")"
    python3 - "$binary" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1])
assert path.read_bytes()[:4] == b'\x7fELF', str(path)
print(path, 'native ELF entry point')
PY
  done
  "${bazel_cmd[@]}" test "${flags[@]}" --nocache_test_results --strategy=TestRunner=local --test_output=errors \
    "--test_env=RUNTIME=$w/runtime/runsc" "--test_env=GVISOR_SIDECAR_BINARIES_DIR=$w/runtime/gvisor-bin" \
    "--test_env=GTEST_FILTER=$filter" --test_arg=--one-sandbox=false \
    "--build_event_json_file=$RESULT_DIR/$group.bep" "${targets[@]}" 2>&1 | tee "$RESULT_DIR/$group-test.log"
  logs="$("${bazel_cmd[@]}" info bazel-testlogs)"
  for target in "${targets[@]}"; do
    name="${target##*:}"
    cp "$logs/test/syscalls/$name/test.xml" "$RESULT_DIR/$name.xml"
    cp "$logs/test/syscalls/$name/test.log" "$RESULT_DIR/$name.log"
  done
done
python3 - "$RESULT_DIR" <<'PY'
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
root=Path(sys.argv[1]);files=list(root.glob('*_test_native.xml'))+list(root.glob('*_test_runsc_systrap_*.xml'))
assert len(files)==9, [p.name for p in files]
for path in files:
    cases=ET.parse(path).getroot().findall('.//testcase')
    assert cases, path.name
    bad=[c.attrib for c in cases if any(c.find(tag) is not None for tag in ('skipped','failure','error'))]
    assert not bad, (path.name,bad)
    print(path.name,len(cases),'PASS zero skips')
PY
make "${make_args[@]}" lint 2>&1 | tee "$RESULT_DIR/lint.log"
make "${make_args[@]}" lint-cc 2>&1 | tee "$RESULT_DIR/lint-cc.log"
