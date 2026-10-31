#!/bin/bash
set -eu -o pipefail
root=$RUNNER_TEMP/proof
mkdir -p "$root"
src=fuse-before-mount
guest=//test/syscalls:fuse_test_runsc_systrap_directfs
native=//test/syscalls/linux:fuse_test
names=FuseTest.PollBeforeMount:FuseTest.EpollAcrossMount
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
  sha256sum test/syscalls/linux/fuse.cc pkg/sentry/fsimpl/fuse/dev.go > "$d/source-digests"
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
begin native "$OFIXED"
run build "${bz[@]}" build "${common[@]}" -c opt --jobs=8 "$native"
if [[ $(cat "$d/build.exit") == 0 ]]; then
  docker exec overlay-guest cp bazel-bin/test/syscalls/linux/fuse_test "$RUNNER_TEMP/fuse-native"
  if [[ ! -e /dev/fuse ]]; then sudo modprobe fuse || :; fi
  if [[ ! -e /dev/fuse ]]; then sudo mknod /dev/fuse c 10 229; fi
  uname -sr > "$d/kernel"
  run native sudo unshare --mount --propagation private timeout 90 "$RUNNER_TEMP/fuse-native" "--gtest_filter=$names" "--gtest_output=xml:$d/test.xml"
  cp "$RUNNER_TEMP/$src-native.log" "$d/test.log"
  run unavailable sudo unshare --mount --propagation private timeout 90 bash -c 'mount -t tmpfs tmpfs /dev && exec "$@"' -- "$RUNNER_TEMP/fuse-native" --gtest_filter=FuseTest.PollBeforeMount "--gtest_output=xml:$d/unavailable.xml"
  cp "$RUNNER_TEMP/$src-unavailable.log" "$d/unavailable.log"
fi
for name in stock fixed; do
  source=$OFIXED; [[ $name != stock ]] || source=$OSTOCK
  begin "$name" "$source"
  run "$name" "${bz[@]}" test "${common[@]}" "${tests[@]}" -c opt --jobs=8 --test_timeout=90 "--test_env=GTEST_FILTER=$names" "$guest"
  if [[ $(cat "$d/$name.exit") =~ ^(0|3)$ ]]; then
    get_logs test/syscalls/fuse_test_runsc_systrap_directfs guest
  fi
done
