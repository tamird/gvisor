#!/bin/bash
set -eu -o pipefail
root=$RUNNER_TEMP/proof
mkdir -p "$root"
src=gofer-dgram
guest=//test/syscalls:connect_external_test_runsc_systrap_directfs
native=//test/syscalls:connect_external_test_native
names=GoferDatagramEpollTest.WakesAfterHostDrain
common=(--config=x86_64 --remote_download_outputs=toplevel --strategy=GoStaticAnalysis=linux-sandbox --strategy=GoStandardLibraryAnalysis=linux-sandbox --strategy=GoStdlib=linux-sandbox --strategy=GoStdlibList=linux-sandbox '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache,^TestRunner$=+no-remote-cache')
tests=(--strategy=TestRunner=remote --nocache_test_results '--remote_download_regex=.*[/]test[.](log|xml)$' --test_output=all --test_sharding_strategy=disabled)
begin() {
  local name=$1 source=$2
  d="$root/$name"
  mkdir -p "$d"
  git checkout --quiet --detach "$source"
  git rev-parse HEAD > "$d/source"
  git rev-parse HEAD^ > "$d/parent"
  git status --porcelain --untracked-files=no > "$d/before-status"
  sha256sum test/syscalls/linux/connect_external.cc test/uds/uds.go pkg/sentry/fsimpl/gofer/socket.go pkg/sentry/socket/unix/transport/{connectionless,host}.go > "$d/source-digests"
}
run() {
  local log="$RUNNER_TEMP/$src-$1.log" id=$1; shift
  local rc=0 keeper
  printf '%s\n' "$@" > "$d/$id.argv"
  date -u +%FT%T.%3NZ > "$d/$id.start"
  "$@" > "$log" 2>&1 & local pid=$!
  (while sleep 40; do echo "heartbeat $id $(date -u +%FT%TZ) $(wc -c < "$log") bytes"; tail -c 700 "$log"; done) & keeper=$!
  wait "$pid" || rc=$?
  kill "$keeper" 2>/dev/null || :
  wait "$keeper" 2>/dev/null || :
  echo "$rc" > "$d/$id.exit"
  date -u +%FT%T.%3NZ > "$d/$id.end"
  grep -A7 -B1 -E 'Invocation ID:|^ERROR:| FAILED | PASSED |SKIPPED|^FAIL:|^Target |error: |^\[ ( RUN| *OK| *FAILED| *SKIPPED)' "$log" | tail -220 > "$d/$id.diagnostics" || :
  git status --porcelain --untracked-files=no > "$d/after-status"
}
bz=(docker exec -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0="$GITHUB_WORKSPACE" overlay-guest bazel "--bazelrc=$RUNNER_TEMP/overlay.bazelrc")
get_logs() {
  local p=$1 key=$2
  for f in test.log test.xml; do
    docker exec overlay-guest cat "bazel-testlogs/$p/$f" > "$d/$key.$f" 2>/dev/null || rm -f "$d/$key.$f"
  done
}
for arm in native stock fixed; do
  source=$OFIXED; [[ $arm != stock ]] || source=$OSTOCK
  begin "$arm" "$source"
  target=$guest; [[ $arm != native ]] || target=$native
  run "$arm" "${bz[@]}" test "${common[@]}" "${tests[@]}" -c opt --jobs=8 --test_timeout=120 "--test_env=GTEST_FILTER=$names" "$target"
  if [[ $(cat "$d/$arm.exit") =~ ^(0|3)$ ]]; then
    name=${target##*:}
    get_logs "test/syscalls/$name" guest
  fi
done
begin owners "$OFIXED"
transport=//pkg/sentry/socket/unix/transport
gofer=//pkg/sentry/fsimpl/gofer
run unit "${bz[@]}" test "${common[@]}" "${tests[@]}" --jobs=8 --test_arg=-test.v "$transport:transport_test" "$gofer:gofer_test"
for t in "$transport:transport_test" "$gofer:gofer_test"; do path=${t#//}; get_logs "${path/:/\/}" "${t##*:}"; done
run owners "${bz[@]}" test "${common[@]}" "${tests[@]}" --jobs=6 --keep_going  --output_groups=default,nogo_findings --test_timeout=900 "$gofer:gofer_nogo" "$transport:transport_nogo"
for t in "$gofer:gofer_nogo" "$transport:transport_nogo"; do
  name=${t##*:}; path=${t#//}; path=${path%:*}; get_logs "$path/$name" "$name"
  for arch in amd64 arm64; do f="$name.$arch.findings"; docker exec overlay-guest sha256sum "bazel-bin/$path/$f" > "$d/$f.sha256" || :; done
done
