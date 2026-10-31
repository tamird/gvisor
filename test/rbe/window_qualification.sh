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
if [[ $1 == units ]]; then
  stock_img=$(image_from "$RUNNER_TEMP/records/stock/state.test.log" || :)
  name=mutant
  record "$MUTANT"
  run_bazel test "${common[@]}" --jobs=16 --test_arg=-test.v --test_arg=-test.run='^TestGrowingSliceWindows$/^graph$/^new_tail$' //pkg/state/tests:tests_test
  test_artifacts pkg/state/tests/tests_test generated
  name=fixed
  record "$SOURCE"
  run_bazel test "${common[@]}" --jobs=16 --keep_going --test_arg=-test.v "--test_env=WINDOW_PEER_IMAGE=$stock_img" //pkg/state:state_test //pkg/state/tests:tests_test
  test_artifacts pkg/state/state_test state
  test_artifacts pkg/state/tests/tests_test generated
  fixed_img=$(image_from "$d/state.test.log" || :)
  if [[ -n $stock_img && -n $fixed_img && $stock_img != "$fixed_img" ]]; then
    name=stock_read
    record "$STOCK"
    run_bazel test "${common[@]}" --jobs=16 --test_arg=-test.v --test_arg=-test.run='^TestWindowInterop$' "--test_env=WINDOW_PEER_IMAGE=$fixed_img" //pkg/state:state_test
    test_artifacts pkg/state/state_test state
  fi
  name=counters
  record "$COUNTERS"
  run_bazel test "${common[@]}" --jobs=16 --test_arg=-test.v --test_arg=-test.run='^TestGraphGrowingWindows$' //pkg/state:state_test
  test_artifacts pkg/state/state_test state
else
  name=nginx
  record "$GITHUB_SHA"
  image=test/rbe/window_nginx/nginx.tar
  docker pull --platform=linux/amd64 "$NGINX"
  digest=$(docker image inspect --format '{{.Id}}' "$NGINX")
  echo "$digest" > "$d/nginx-image"
  [[ $digest == sha256:5f9e05caa37420ad50b7ccf873ea117fcbd8cff24b3fcb689cb904bcab624cf5 ]]
  docker tag "$NGINX" gvisor.dev/images/benchmarks/nginx:latest
  test ! -e "$image"
  docker save -o "$image" gvisor.dev/images/benchmarks/nginx:latest
  trap 'rm -f "$image"' EXIT
  run_bazel test "${common[@]}" --jobs=32 --test_timeout=1800 -c opt //test/rbe/window_nginx:checkpoint
  test_artifacts test/rbe/window_nginx/checkpoint nginx
fi
