#!/bin/bash
set -euo pipefail
w="$RUNNER_TEMP/atomic-systrap-native"
results="$w/results"
status=0
make_args=(DOCKER_BUILD=false "BAZEL=$w/bin/bazel --host_jvm_args=-Xmx4g" "BAZEL_OPTIONS=--config=$QUAL_CONFIG --jobs=3")
run() {
  local name="$1" rc=0
  shift
  "$@" 2>&1 | tee "$results/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$name" "$rc" >> "$results/commands.tsv"
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
trap '"$w/bin/bazel" --host_jvm_args=-Xmx4g shutdown || true' EXIT
printf '%s\n' "$SOURCE" > "$results/source.txt"
printf '%s\n' "$BASE" > "$results/base.txt"
uname -a > "$results/worker.txt"
for arm in base candidate; do
  revision="$BASE"
  if test "$arm" = candidate; then revision="$SOURCE"; fi
  git diff --exit-code
  git switch --detach "$revision"
  run "$arm-stub" make "${make_args[@]}" copy TARGETS=//pkg/sentry/platform/systrap/sysmsg:sighandler_binary "DESTINATION=$results/$arm.stub" || continue
  sha256sum "$results/$arm.stub" > "$results/$arm.stub.sha256"
done
git switch --detach "$SOURCE"
if test "$status" != 0; then
  echo 'Stub compilation failed; runtime qualification cannot proceed.' > "$results/performance.txt"
  exit "$status"
fi
run build make "${make_args[@]}" build TARGETS='//runsc //pkg/sentry/platform/systrap:systrap_test' || true
run systrap-tests make "${make_args[@]}" test TARGETS=//pkg/sentry/platform/systrap:systrap_test OPTIONS='--nocache_test_results --strategy=TestRunner=local' || true
if test -f .build_events.json; then cp .build_events.json "$results/systrap-tests.bep"; fi
targets='//test/syscalls:fpsig_fork_test_runsc_systrap_shared //test/syscalls:fpsig_nested_test_runsc_systrap_shared'
if test "$QUAL_ARCH" = amd64; then
  targets+=' //test/syscalls:fpsig_mut_test_runsc_systrap_shared //test/syscalls:fpstate_stop_test_runsc_systrap_shared'
fi
run fp-syscalls make "${make_args[@]}" syscall-tests "TARGETS=$targets" "RUNTIME_DIR=$w/runtime" OPTIONS='--nocache_test_results --strategy=TestRunner=local' || true
if test -f .build_events.json; then cp .build_events.json "$results/fp-syscalls.bep"; fi
run lint make "${make_args[@]}" lint || true
if test "$QUAL_ARCH" = amd64; then
  run lint-cc make "${make_args[@]}" lint-cc || true
else
  echo 'ARM64 lint-cc previously failed in unchanged test/syscalls/linux/io_uring.cc (misc-include-cleaner). This run does not claim that baseline clean; the complete C++ lint command runs on AMD64.' > "$results/arm64-lint-scope.txt"
fi
if test -f "$results/base.stub" && test -f "$results/candidate.stub" && cmp -s "$results/base.stub" "$results/candidate.stub"; then
  echo 'IDENTICAL: the complete emitted stub blobs match on the same compiler and base; the changed C translation units introduce no runtime instruction or data difference.' > "$results/performance.txt"
elif test -f "$results/base.stub" && test -f "$results/candidate.stub"; then
  echo 'DIFFERENT: emitted-code equivalence does not establish performance parity. Matched existing benchmarks follow; retain raw samples for benchstat.' > "$results/performance.txt"
  # Only these selected benchmarks need images; avoid load-benchmarks pulling
  # every unrelated image via the canonical benchmark-platforms prerequisite.
  run alpine-image make "${make_args[@]}" load-benchmarks_alpine || true
  if test "$QUAL_ARCH" = amd64; then
    run syscallbench-image make "${make_args[@]}" load-benchmarks_syscallbench || true
  fi
  # Four alternating six-sample batches give 24 observations per arm, with
  # both source revisions using the same compiler, machine and Make path.
  for batch in 1 2 3 4; do
    order='base candidate'
    if test $((batch % 2)) = 0; then order='candidate base'; fi
    for arm in $order; do
      revision="$BASE"
      if test "$arm" = candidate; then revision="$SOURCE"; fi
      git diff --exit-code
      git switch --detach "$revision"
      run "$arm-startup-$batch" make -o load-benchmarks "${make_args[@]}" benchmark-platforms BENCHMARKS_TARGETS=//test/benchmarks/base:startup_test BENCHMARKS_PLATFORMS=systrap BENCHMARKS_RUNC=false BENCHMARKS_FILTER='^BenchmarkStartupEmpty$' BENCHMARKS_OPTIONS='-test.benchtime=1x -test.count=6' "RUNTIME_DIR=$w/runtime" || true
      if test "$QUAL_ARCH" = amd64; then
        run "$arm-syscallbench-$batch" make -o load-benchmarks "${make_args[@]}" benchmark-platforms BENCHMARKS_TARGETS=//test/benchmarks/base:syscallbench_test BENCHMARKS_PLATFORMS=systrap BENCHMARKS_RUNC=false BENCHMARKS_FILTER='.' BENCHMARKS_OPTIONS='-test.benchtime=1s -test.count=6' "RUNTIME_DIR=$w/runtime" || true
      fi
    done
  done
  for arm in base candidate; do
    cat "$results/$arm-startup-"*.log > "$results/$arm-benchmarks.txt"
    if test "$QUAL_ARCH" = amd64; then cat "$results/$arm-syscallbench-"*.log >> "$results/$arm-benchmarks.txt"; fi
  done
  # Install the pinned external analysis tool outside the gVisor checkout.
  # All gVisor binaries and benchmarks above are built by Bazel.
  if (cd "$w" && GOBIN="$w/bin" GOWORK=off go install golang.org/x/perf/cmd/benchstat@4febdb082b2ce2a0f9f6f496698aa5a2273279b8); then
    go version -m "$w/bin/benchstat" > "$results/benchstat-version.txt"
    run benchstat "$w/bin/benchstat" "$results/base-benchmarks.txt" "$results/candidate-benchmarks.txt" || true
  else
    echo 'Pinned benchmark analysis tool installation failed.' >> "$results/performance.txt"
    status=1
  fi
else
  echo 'UNAVAILABLE: a stub build failed; do not spend benchmark time before fixing that failure.' > "$results/performance.txt"
  status=1
fi
git switch --detach "$SOURCE"
exit "$status"
