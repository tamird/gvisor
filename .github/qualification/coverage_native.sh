#!/bin/bash
set -euo pipefail
w="$RUNNER_TEMP/coverage-native"
results="$w/results"
status=0
mkdir -p "$w/binaries"
bazel_cmd=("$w/bin/bazel" --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=("--config=$QUAL_CONFIG" --jobs=3 --strip=never --nostamp --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false "BAZEL=$w/bin/bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc" "BAZEL_OPTIONS=${flags[*]}")
run() {
  local name="$1" rc=0
  shift
  "$@" 2>&1 | tee "$results/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$name" "$rc" >> "$results/commands.tsv"
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
path_of() {
  "${bazel_cmd[@]}" cquery "${flags[@]}" --output=files "$1" > "$w/path"
  test "$(wc -l < "$w/path")" -eq 1
  realpath "$(cat "$w/path")"
}
trap '"$w/bin/bazel" --host_jvm_args=-Xmx4g shutdown || true' EXIT
printf '%s\n' "$SOURCE" > "$results/source.txt"
printf '%s\n' "$BASE" > "$results/base.txt"
uname -a > "$results/worker.txt"
# Reuse completed f113 qualification run 44176467049 for normal code/data
# parity, all four coverage binary builds/version/symbolization/ARM constructors,
# and candidate lint results. This follow-up tests only the runner report-mode
# correction. The binaries must still be built here to exercise KCOV.
coverage=(//runsc:runsc_coverage //runsc:runsc_race_coverage //runsc/cmd/sentry:gvisor_sentry_coverage //runsc/cmd/sentry:gvisor_sentry_race_coverage)
if run coverage-build "${bazel_cmd[@]}" build "${flags[@]}" "${coverage[@]}"; then
  for label in "${coverage[@]}"; do
    name="${label##*:}"
    cp "$(path_of "$label")" "$w/binaries/$name"
  done
  # Stage the complete release, then substitute only the two known test slots.
  # make syscall-tests would rebuild its phony runtime prerequisite and replace
  # the coverage sidecar, so use canonical make test with explicit runtime paths.
  runtime="$w/runtime"
  mkdir -p "$runtime"
  if run release-install make "${make_args[@]}" copy TARGETS=//:release "DESTINATION=$runtime/"; then
    targets='//test/syscalls:kcov_test_runsc_systrap_directfs //test/syscalls:kcov_test_runsc_systrap_shared'
    for mode in normal race; do
      runsc=runsc_coverage
      sentry=gvisor_sentry_coverage
      if test "$mode" = race; then runsc=runsc_race_coverage; sentry=gvisor_sentry_race_coverage; fi
      cp --remove-destination "$w/binaries/$runsc" "$runtime/runsc"
      cp --remove-destination "$w/binaries/$sentry" "$runtime/gvisor-bin/gvisor_sentry"
      sha256sum "$runtime/runsc" "$runtime/gvisor-bin/gvisor_sentry" > "$results/$mode-installed.sha256"
      run "$mode-kcov" make "${make_args[@]}" test "TARGETS=$targets" "OPTIONS=--nocache_test_results --strategy=TestRunner=local --test_arg=--collect-coverage=false --test_env=RUNTIME=$runtime/runsc --test_env=GVISOR_SIDECAR_BINARIES_DIR=$runtime/gvisor-bin" || true
      if test -f .build_events.json; then cp .build_events.json "$results/$mode-kcov.bep"; fi
      logs="$("${bazel_cmd[@]}" info bazel-testlogs)"
      for label in $targets; do
        name="${label##*:}"
        if test -f "$logs/test/syscalls/$name/test.xml"; then
          cp "$logs/test/syscalls/$name/test.xml" "$results/$mode-$name.xml"
        else
          echo "Missing XML: $mode $name" >> "$results/missing-xml.txt"
          status=1
        fi
        if test -f "$logs/test/syscalls/$name/test.log"; then cp "$logs/test/syscalls/$name/test.log" "$results/$mode-$name.log"; fi
      done
    done
    if ! python3 - "$results" <<'PY'
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
root=Path(sys.argv[1]);files=list(root.glob('*-kcov_test_*.xml'))
assert len(files)==4, [str(p) for p in files]
for path in files:
    cases=ET.parse(path).getroot().findall('.//testcase')
    assert cases, str(path)
    failed=[case.attrib for case in cases if any(case.find(tag) is not None for tag in ('skipped','failure','error'))]
    assert not failed, (str(path), failed)
    print(path.name, len(cases), 'PASS, zero skips')
PY
    then status=1; fi
  fi
fi
for mode in normal race; do
  for access in directfs shared; do
    log="$results/$mode-kcov_test_runsc_systrap_$access.log"
    grep -q -- '-debug-log ' "$log" || status=1
    if grep -q -- '-coverage-report ' "$log"; then status=1; fi
  done
done
# The runner flag is new maintained behavior; qualify its existing unit owner.
run runner-owner make "${make_args[@]}" test TARGETS=//test/runner:runner_test OPTIONS='--nocache_test_results --strategy=TestRunner=local' || true
# Keep an ordinary default-on control; its test log must retain coverage-report
# and debug-log. KCOV tests above require zero skips with report mode disabled.
run default-report-control make "${make_args[@]}" test TARGETS=//test/syscalls:kcov_test_runsc_systrap_directfs "OPTIONS=--nocache_test_results --strategy=TestRunner=local --test_env=RUNTIME=$runtime/runsc --test_env=GVISOR_SIDECAR_BINARIES_DIR=$runtime/gvisor-bin" || true
logs="$("${bazel_cmd[@]}" info bazel-testlogs)"
cp "$logs/test/syscalls/kcov_test_runsc_systrap_directfs/test.log" "$results/default-report-control.test.log"
grep -q -- '-coverage-report ' "$results/default-report-control.test.log" || status=1
grep -q -- '-debug-log ' "$results/default-report-control.test.log" || status=1
run lint make "${make_args[@]}" lint || true
run lint-cc make "${make_args[@]}" lint-cc || true
if test "$QUAL_ARCH" = arm64; then
  git diff --exit-code
  git switch --detach "$BASE"
  run base-lint-cc make "${make_args[@]}" lint-cc || true
  git switch --detach "$SOURCE"
fi
# Preserve any known baseline lint failure as failure, never relabel it clean.
exit "$status"
