#!/bin/bash
set -euo pipefail
set_source() {
  local src=$1
  git checkout --quiet --detach "$src"
  [[ $(git rev-parse HEAD) == "$src" ]]
  git rev-parse HEAD > "$d/source"
  git rev-parse HEAD^ > "$d/parent"
  git cat-file commit HEAD > "$d/commit"
  git status --porcelain --untracked-files=no > "$d/source-before-status"
  sha256sum pkg/state/graph_decode_callbacks_test.go > "$d/fixture-sha256"
  sha256sum pkg/state/graph_encode_objects_unsafe.go > "$d/product-sha256"
}
run_bazel() {
  local cmd=(docker exec tcp-compiler bazel --bazelrc="$RUNNER_TEMP/tcp.bazelrc" "$@")
  local logfile="$RUNNER_TEMP/$name-bazel.log" rc=0 heartbeat
  printf '%s\n' "$@" > "$d/argv"
  date -u +%FT%T.%3NZ > "$d/start"
  "${cmd[@]}" > "$logfile" 2>&1 &
  local pid=$!
  (while sleep 40; do printf 'heartbeat %s %s, %s bytes of Bazel output\n' "$name" "$(date -u +%FT%TZ)" "$(wc -c < "$logfile")"; done) &
  heartbeat=$!
  wait "$pid" || rc=$?
  kill "$heartbeat" 2>/dev/null || :
  wait "$heartbeat" 2>/dev/null || :
  printf '%s\n' "$rc" > "$d/exit"
  date -u +%FT%T.%3NZ > "$d/end"
  git rev-parse HEAD > "$d/source-after"
  git status --porcelain --untracked-files=no > "$d/source-after-status"
  grep -A8 -E 'Invocation ID:|ERROR:|Failed to|failed:|FAILED|SKIPPED| PASSED|Build did NOT complete' "$logfile" | tail -180 > "$d/bazel-diagnostics" || :
}
common=(--config=x86_64 --verbose_failures --nocache_test_results --remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$' --test_sharding_strategy=disabled --test_output=all)
for name in base counters; do
  d="$RUNNER_TEMP/records/$name"
  mkdir -p "$d"
  src=$SOURCE
  [[ $name != counters ]] || src=$COUNTERS
  set_source "$src"
  run_bazel test "${common[@]}" --jobs=16 --test_arg=-test.v --test_arg=-test.run='^TestGraphGrowingWindows$' //pkg/state:state_test
  for f in test.log test.xml; do
    docker exec tcp-compiler cat "bazel-testlogs/pkg/state/state_test/$f" > "$d/$f" || rm -f "$d/$f"
  done
done
