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
  sha256sum test/benchmarks/checkpointfile/checkpointfile.go > "$d/fixture-sha256"
  sha256sum pkg/state/graph_decode_unsafe.go > "$d/product-sha256"
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
name=stock-build
record "$STOCK"
run_bazel build --config=x86_64 -c opt --jobs=32 --remote_download_outputs=toplevel //:release
[[ $(cat "$d/exit") == 0 ]]
docker exec tcp-compiler test -x bazel-bin/release/gvisor-bin/gvisor_sentry
docker exec tcp-compiler tar czfh "$RUNNER_TEMP/stock.tgz" -C bazel-bin/release .
docker exec tcp-compiler sha256sum bazel-bin/release/runsc bazel-bin/release/gvisor-bin/gvisor_sentry > "$d/runtime-identity"
  name=nginx
  record "$GITHUB_SHA"
  mkdir test/rbe/window_nginx/stock-release
  tar xzf "$RUNNER_TEMP/stock.tgz" -C test/rbe/window_nginx/stock-release
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
