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
  grep -A8 -E 'Invocation ID:|[.]go:[0-9]+(:[0-9]+)?:|not enough arguments|Failed to|ERROR:|failed:|FAILED|SKIPPED| PASSED|Build did NOT complete' "$logfile" | tail -180 > "$d/bazel-diagnostics" || :
}
common=(--config=x86_64 --verbose_failures --nocache_test_results --remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$' --test_sharding_strategy=disabled --test_output=all)
record() {
  local src=$1
  d="$RUNNER_TEMP/records/$name"
  mkdir -p "$d"
  set_source "$src"
}
test_artifacts() {
  local path=$1 prefix=$2 code
  code=$(cat "$d/exit")
  [[ $code == 0 || $code == 3 ]] || return 0
  for file in test.log test.xml; do
    docker exec tcp-compiler cat "bazel-testlogs/$path/$file" > "$d/$prefix.$file" || rm -f "$d/$prefix.$file"
  done
}
image_from() {
  sed -n 's/.*WINDOW-IMAGE hex=\([0-9a-f]*\)$/\1/p' "$1" | head -1
}
name=stock_shift
record "$STOCK_SHIFT"
run_bazel test "${common[@]}" --jobs=16 --test_arg=-test.v --test_arg=-test.run='^TestGraphDeferredObjectDecode$/^multi_pass_round_trip$' //pkg/state:state_test
test_artifacts pkg/state/state_test state

name=full
record "$SOURCE"
run_bazel test "${common[@]}" --jobs=16 --keep_going --test_arg=-test.v //pkg/state:state_test //pkg/state/tests:tests_test
test_artifacts pkg/state/state_test state
test_artifacts pkg/state/tests/tests_test generated

stock_img=$(image_from "$RUNNER_TEMP/records/stock/state.test.log" || :)
name=fixed_read
record "$SOURCE"
run_bazel test "${common[@]}" --jobs=16 --test_arg=-test.v --test_arg=-test.run='^TestWindowInterop$' "--test_env=WINDOW_PEER_IMAGE=$stock_img" //pkg/state:state_test
test_artifacts pkg/state/state_test state

fixed_img=$(image_from "$d/state.test.log" || :)
name=stock_read
record "$STOCK"
run_bazel test "${common[@]}" --jobs=16 --test_arg=-test.v --test_arg=-test.run='^TestWindowInterop$' "--test_env=WINDOW_PEER_IMAGE=$fixed_img" //pkg/state:state_test
test_artifacts pkg/state/state_test state
