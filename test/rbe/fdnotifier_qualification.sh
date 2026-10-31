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
  sha256sum pkg/fdnotifier/fdnotifier_test.go > "$d/fixture-sha256"
  sha256sum pkg/fdnotifier/fdnotifier.go > "$d/product-sha256"
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
if [[ $1 == unit ]]; then
  for name in control correction; do
    d="$RUNNER_TEMP/records/$name"; mkdir -p "$d"
    src=$FIXED; [[ $name != control ]] || src=$CONTROL
    set_source "$src"
    run_bazel test "${common[@]}" --jobs=32 --strategy=TestRunner=local --test_arg=-test.v --test_arg=-test.run='^TestStaleEpollEvent$' //pkg/fdnotifier:fdnotifier_test
    for f in test.log test.xml; do
      docker exec tcp-compiler cat "bazel-testlogs/pkg/fdnotifier/fdnotifier_test/$f" > "$d/$f" || rm -f "$d/$f"
    done
  done
else
  name=owners; d="$RUNNER_TEMP/records/$name"; mkdir -p "$d"
  set_source "$FIXED"
  run_bazel test "${common[@]}" --jobs=4 --keep_going --strategy=GoStaticAnalysis=linux-sandbox --strategy=GoStandardLibraryAnalysis=linux-sandbox --strategy=GoStdlib=linux-sandbox --strategy=GoStdlibList=linux-sandbox '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache' --output_groups=default,nogo_findings --test_timeout=600 //pkg/fdnotifier:fdnotifier_nogo //pkg/fdnotifier:fdnotifier_test_nogo
  for owner in fdnotifier_nogo fdnotifier_test_nogo; do
    for ext in log xml; do
      docker exec tcp-compiler cat "bazel-testlogs/pkg/fdnotifier/$owner/test.$ext" > "$d/$owner.$ext" || rm -f "$d/$owner.$ext"
    done
    for arch in amd64 arm64; do
      key="$owner.$arch.findings"
      if digest=$(docker exec tcp-compiler sha256sum "bazel-bin/pkg/fdnotifier/$key"); then
        printf '%s\n' "${digest%% *}" > "$d/$key.sha256"
      fi
    done
  done
fi
