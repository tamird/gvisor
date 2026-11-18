#!/bin/bash
# Artifact-only diagnosis: no benchmarks, tests, source edits, or tool installs.
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 5a397632d896158997ee7594866b33d741c56917
test "$BASE" = bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa
test "$(git show -s --format=%P "$SOURCE")" = "$BASE"
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
git diff --quiet --exit-code
git diff --cached --quiet --exit-code
support=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/systrap-layout"
mkdir -m 700 "$w"
mkdir -p "$RESULT_DIR/layout"
result="$RESULT_DIR/layout"
for tool in python3 readelf nm objdump tar timeout; do command -v "$tool"; done
cleanup() {
  local rc=$?
  trap - EXIT
  git switch --detach "$SOURCE" || rc=1
  git diff --quiet --exit-code || rc=1
  git diff --cached --quiet --exit-code || rc=1
  exit "$rc"
}
trap cleanup EXIT
run() {
  local label="$1" cap="$2"
  shift 2
  printf '%q ' "$@" >> "$result/commands.txt"
  printf '\n' >> "$result/commands.txt"
  local rc=0
  timeout --signal=TERM --kill-after=60s "$cap" "$@" 2>&1 | tee "$result/$label.log" || rc=$?
  printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$label" "$rc" >> "$result/status.tsv"
  return "$rc"
}
test "$(df -Pk "$RUNNER_TEMP" | awk 'NR==2 {print $4}')" -ge 12582912
uname -a > "$result/worker.txt"
lscpu > "$result/lscpu.txt"
bazel --version > "$result/bazel-version.txt"
readelf --version > "$result/readelf-version.txt"
nm --version > "$result/nm-version.txt"
objdump --version > "$result/objdump-version.txt"
git diff "$BASE" "$SOURCE" -- pkg/sentry/platform/systrap/sysmsg > "$result/source.patch"
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' 'BAZEL_OPTIONS=--config=x86_64 --jobs=3 --nostamp --color=no --curses=no --remote_executor= --remote_cache= --bes_backend=')
for arm in base candidate; do
  revision="$BASE"
  if test "$arm" = candidate; then revision="$SOURCE"; fi
  git switch --detach "$revision"
  mkdir -m 700 "$w/$arm" "$w/$arm/release" "$result/$arm"
  # Preserve/install the complete canonical release. Its member list is opaque.
  run "$arm-release" 55m make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$w/$arm/release"
  timeout --kill-after=30s 10m tar -C "$w/$arm/release" -czf "$result/$arm/release.tar.gz" .
  # Stub intermediates are evidence, never a partial runtime installation.
  run "$arm-stub" 15m make "${make_args[@]}" copy 'TARGETS=//pkg/sentry/platform/systrap/sysmsg:sighandler_object //pkg/sentry/platform/systrap/sysmsg:sighandler_binary //pkg/sentry/platform/systrap/sysmsg:sighandler //pkg/sentry/platform/systrap/sysmsg:sysmsg_lib' "DESTINATION=$result/$arm"
  run "$arm-actions" 5m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc aquery --config=x86_64 --jobs=3 --nostamp --color=no --curses=no --output=text --include_commandline --remote_executor= --remote_cache= --bes_backend= 'deps(//pkg/sentry/platform/systrap/sysmsg:sighandler_object)'
  git show "$revision:pkg/sentry/platform/systrap/sysmsg/build.bzl" > "$result/$arm/build.bzl.txt"
  git show "$revision:tools/bazeldefs/cc.bzl" > "$result/$arm/cc.bzl.txt"
  git show "$revision:pkg/sentry/platform/systrap/sysmsg/BUILD" > "$result/$arm/BUILD.txt"
  timeout --kill-after=30s 15m python3 "$support/inspect.py" "$w/$arm/release" "$result/$arm" "$result/$arm/sighandler.built-in.bin.o" "$result/$arm/sighandler.built-in.bin" "$result/$arm/sighandler.o" "$result/$arm/sysmsg_lib.o"
  test "$(du -sm "$w" "$result" | awk '{total += $1} END {print total}')" -le 8192
done
timeout --kill-after=30s 2m python3 "$support/compare.py" "$result"
cat > "$result/preprocessing-limitation.txt" <<'LIMITATION'
No project compilation was invoked outside Bazel. No preprocessor output was generated.
The sysmsg cc_pie_obj rule is a genrule with a literal compiler command.
Its cc_flags_supplier supplies CC_FLAGS="", so ordinary --copt flags are
not a supported route to -H, -E, -dM or -save-temps for these actions.
Retained aquery commands and rule sources permit verification of that limitation.
The selected toolchain's transitive standard-header macros remain unknown;
this run cannot establish whether the atomic renames are required by new headers.
Resolving that requires a separately source-bound Bazel diagnostic variant;
this run does not supply it. Existing adverse benchmark results are neither superseded nor resolved.
LIMITATION
timeout --kill-after=30s 5m python3 - "$result" "$SOURCE" "$BASE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
files = {}
for path in sorted(root.rglob('*')):
    if path.is_file():
        with path.open('rb') as stream:
            files[str(path.relative_to(root))] = hashlib.file_digest(stream, 'sha256').hexdigest()
(root / 'manifest.json').write_text(json.dumps({'source': sys.argv[2], 'base': sys.argv[3],
    'mode': 'artifact-only', 'stamp': False, 'benchmarksRun': False, 'files': files}, indent=2) + '\n')
PY
