#!/bin/bash
set -euo pipefail
for arm in native master approved; do
  src="$CONTROL"; test "$arm" != approved || src="$FIXED"
  platform=runsc_systrap_directfs; executor=remote
  if [[ $arm == native ]]; then platform=native; executor=local; fi
  d="$RUNNER_TEMP/records/$arm"; mkdir -p "$d"
  git checkout --detach --quiet "$src"
  test "$(git rev-parse HEAD)" = "$src"
  git rev-parse HEAD > "$d/source"
  git diff --exit-code HEAD
  sha256sum test/uds/uds.go test/syscalls/linux/connect_external.cc > "$d/fixtures"
  sha256sum pkg/sentry/syscalls/linux/sys_socket.go > "$d/product"
  target="//test/syscalls:connect_external_test_$platform"
  filter=GoferStreamTest.ReleaseUndeliveredHostRights
  args=(test --config=x86_64 --jobs=64 --verbose_failures "--strategy=TestRunner=$executor" --nocache_test_results '--modify_execution_info=^TestRunner$=+no-remote-cache' --remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$' --test_output=all --test_timeout=300 --test_sharding_strategy=disabled "--test_env=GTEST_FILTER=$filter" "$target")
  printf '%s\n' "${args[@]}" > "$d/argv"
  date -u +%FT%T.%3NZ > "$d/start"
  rc=0
  logfile="$RUNNER_TEMP/$arm.log"
  docker exec tcp-compiler bazel --bazelrc="$RUNNER_TEMP/tcp.bazelrc" "${args[@]}" > "$logfile" 2>&1 &
  child=$!
  (while sleep 45; do echo "$arm $(date -u +%FT%TZ) $(wc -c < "$logfile") bytes"; done) & reporter=$!
  wait "$child" || rc=$?
  kill "$reporter" 2>/dev/null || :
  wait "$reporter" 2>/dev/null || :
  date -u +%FT%T.%3NZ > "$d/end"
  printf '%s\n' "$rc" > "$d/exit"
  git diff --exit-code HEAD
  grep -A5 -E 'Invocation ID:|^ERROR:|error:|FAILED|SKIPPED|Build did NOT complete' "$logfile" | tail -125 > "$d/diagnostics" || :
  if [[ $rc != 0 && $rc != 3 ]]; then continue; fi
  for name in test.log test.xml; do
    docker exec tcp-compiler cat "bazel-testlogs/test/syscalls/${target#*:}/$name" > "$d/$name" || rm -f "$d/$name"
  done
done
