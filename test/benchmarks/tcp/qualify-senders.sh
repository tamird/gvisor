#!/bin/bash
set -euo pipefail
rc=$1
base=test/benchmarks/tcp
src=pkg/tcpip/transport/tcp
out=$RUNNER_TEMP/tcp-public
mkdir -p "$out/controls"
export XTABLES_LOCKFILE=$RUNNER_TEMP/qualification-xtables.lock
touch "$XTABLES_LOCKFILE"
printf '%s\n' 0ccb351638e1a53b35967474598afd0e71ddbf13 610df167b9d239a227dd4d220f6185b38518b05d 597f36270 52ed6dad4b746b3eb9eb109e40ca3286e67b7d3e "$GITHUB_SHA" > "$out/source-heads"
cp "$base/sender-sources.txt" "$out/"
uname -a > "$out/worker.txt"
select_sender() {
  local arm=$1 expected
  git restore --source=HEAD --worktree -- "$src/"{snd.go,rack.go,segment.go,sack_scoreboard.go}
  if test "$arm" != reference; then git apply "$base/sender-prr.patch"; fi
  if test "$arm" = accounting; then git apply "$base/accounting-only.patch"; fi
  expected=$(awk -v arm="$arm" '$1 == arm {print $2}' "$base/sender-sources.txt")
  test "$(git hash-object "$src/snd.go")" = "$expected"
  git diff --exit-code HEAD -- "$src/test/" test/packetimpact/tests
}
test_one() {
  local arm=$1 label=$2 filter=$3 required=$4
  local target=${label##*:} package=${label%:*}
  local dest=$out/controls/$arm/${package#//}/$target
  local bep=$RUNNER_TEMP/test-events.jsonl
  mkdir -p "$dest"
  local code=0
  bazelisk --bazelrc="$rc" test --config=x86_64 --lockfile_mode=off --strategy=TestRunner=local \
    --test_sharding_strategy=disabled --nocache_test_results --jobs=4 --test_timeout=180 \
    --test_env=GO_TEST_WRAP_TESTV=1 --test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated --test_env=XTABLES_LOCKFILE \
    --define=gvisor_test_clock_source=calibrated --strip=never "--test_filter=^($filter)$" \
    --test_output=errors "--build_event_json_file=$bep" --build_event_json_file_path_conversion=false \
    "$label" > "$RUNNER_TEMP/build-test.log" 2>&1 || code=$?
  local dir=bazel-testlogs/${package#//}/$target
  for file in test.log test.xml; do
    if test -f "$dir/$file"; then cp "$dir/$file" "$dest/$file"; fi
  done
  if test -d "$dir/test.outputs"; then
    for pcap in "$dir"/test.outputs/*.pcap; do
      if test -f "$pcap"; then cp -L "$pcap" "$dest/"; fi
    done
  fi
  echo "$code" > "$dest/exit"
  jq -s '[.[] | select(.testResult != null) | .testResult | {status,cachedLocally,executionInfo:(.executionInfo|{strategy,cachedRemotely,exitCode})}]' "$bep" > "$dest/execution.json" || return 1
  if test "$code" != 0; then tail -45 "$RUNNER_TEMP/build-test.log"; return 1; fi
  jq -e 'length == 1 and all(.[]; .status=="PASSED" and (.cachedLocally!=true) and (.executionInfo.cachedRemotely!=true) and (.executionInfo.strategy|.=="local" or .=="standalone"))' "$dest/execution.json" >/dev/null || return 1
  # Packetimpact XML wraps the benchmark. Both it and the Go endpoint tests
  # retain the names of completed assertions in the original stdout.
  local testname
  for testname in $required; do
    grep -Fq -- "--- PASS: $testname (" "$dest/test.log" || { echo "missing $testname in $dest"; return 1; }
  done
}
e2e=//pkg/tcpip/transport/tcp/test/e2e
packets=//test/packetimpact/tests
status=0
for arm in reference corrected accounting; do
  select_sender "$arm"
  test_one "$arm" "$e2e:tcp_rack_test" 'TestRTOInterruptsSACKRecovery|TestRACKTLPFallbackRTO' 'TestRTOInterruptsSACKRecovery TestRACKTLPFallbackRTO' || status=1
done
test "$status" = 0 || { echo "Reno qualification failed; shared queue not started"; exit 1; }
for arm in reference corrected; do
  select_sender "$arm"
  test_one "$arm" "$e2e:tcp_test" 'TestRepeatedRetransmissionTimeoutThreshold|TestCubicWindowGrowthAfterPacketLoss|TestZeroWindowSend|TestShutdownRead|TestResetDuringClose' 'TestRepeatedRetransmissionTimeoutThreshold TestCubicWindowGrowthAfterPacketLoss TestZeroWindowSend TestShutdownRead TestResetDuringClose' || status=1
  test_one "$arm" "$e2e:tcp_sack_test" 'TestRecoveryEntry|TestSACKRecovery|TestDetectSpuriousRecoveryWithRTO' 'TestRecoveryEntry TestSACKRecovery TestDetectSpuriousRecoveryWithRTO' || status=1
  test_one "$arm" '//pkg/tcpip/transport/tcp:tcp_test' 'TestCubicRTOPreservesPriorWindow' 'TestCubicRTOPreservesPriorWindow' || status=1
  test_one "$arm" "$packets:tcp_rack_netstack_test" 'TestRTOAfterPartialACK|TestRTOInterruptsCubicRecovery|TestRACKWithSACK|TestRACKFeedbackBudget' 'TestRTOAfterPartialACK TestRTOInterruptsCubicRecovery TestRACKWithSACK TestRACKFeedbackBudget' || status=1
done
test "$status" = 0 || { echo "Integration qualification failed; shared queue not started"; exit 1; }
select_sender reference
test_one linux "$packets:tcp_rack_native_test" 'TestRTOAfterPartialACK|TestRTOInterruptsCubicRecovery|TestRACKWithSACK|TestRACKFeedbackBudget' 'TestRTOAfterPartialACK TestRTOInterruptsCubicRecovery TestRACKWithSACK TestRACKFeedbackBudget'
for arm in reference accounting corrected; do
  select_sender "$arm"
  git apply "$base/observe-$arm.patch"
  git diff HEAD -- "$src/" > "$out/$arm-observed-sender.patch"
  dir=$RUNNER_TEMP/tcp-benchmark-helpers/$arm
  mkdir -p "$dir"
  bazelisk --bazelrc="$rc" build --config=x86_64 --lockfile_mode=off --jobs=4 //test/benchmarks/tcp:{tcp_proxy,nsjoin}
  for tool in tcp_proxy nsjoin; do
    binary=$(bazelisk --bazelrc="$rc" cquery --config=x86_64 --lockfile_mode=off --output=files "//test/benchmarks/tcp:$tool")
    test -x "$binary"
    cp "$binary" "$dir/$tool"
  done
  grep -aFq 'WANRTO ns=' "$dir/tcp_proxy"
  grep -aFq 'WANACK ns=' "$dir/tcp_proxy"
  cp "$RUNNER_TEMP/tcp-benchmark-helpers/reference/tcp_proxy" "$dir/tcp_native_proxy"
  cmp "$RUNNER_TEMP/tcp-benchmark-helpers/reference/nsjoin" "$dir/nsjoin"
  sha256sum "$dir"/{tcp_proxy,tcp_native_proxy,nsjoin} >> "$out/helpers.sha256"
done
select_sender reference
bazelisk --bazelrc="$rc" run --config=x86_64 --lockfile_mode=off --jobs=4 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
echo complete > "$out/controls-passed"
