#!/bin/bash
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Fork-only validation through the existing Actions coordinator.
set -euo pipefail
[[ $QUALIFICATION_EXECUTION == local && $QUALIFICATION_ARCH == amd64 ]]
[[ $QUALIFICATION_LANES == benchmarks ]]
[[ $QUALIFICATION_BENCHMARK_TARGET == //test/benchmarks/tcp:tcp_benchmark ]]
[[ $qualification_root_bazel == true ]]
declare -F bazel >/dev/null
out="$RUNNER_TEMP/qualification/tcp-congestion-observations"
mkdir -p "$out"
export out
finish() {
  local result=$?
  git diff --exit-code > "$out/source-after.diff" || result=1
  if ! sudo -n chown -hR -- "$(id -u):$(id -g)" "$out" && (( result == 0 )); then
    result=1
  fi
  printf '%s\n' "$result" > "$out/driver-exit.txt"
  exit "$result"
}
trap finish EXIT
[[ $(git rev-parse HEAD) == "$QUALIFICATION_COMMIT" ]]
git cat-file -p HEAD > "$out/source-commit.txt"
for file in test/benchmarks/tcp/tcp_benchmark.sh test/benchmarks/tcp/tcp_proxy.go test/benchmarks/tcp/README.md test/benchmarks/tcp/BUILD tools/bazeldefs/go.bzl tools/bazeldefs/platforms.bzl .bazelrc pkg/tcpip/transport/tcp/cubic.go pkg/tcpip/transport/tcp/reno.go pkg/tcpip/transport/tcp/snd.go pkg/tcpip/transport/tcp/connect.go test/rbe/actions.sh .github/workflows/build.yml test/rbe/tcp_congestion_observations.sh; do
  cp "$file" "$out/source-${file//\//_}"
done
sudo -n apt-get update > "$out/apt-update.txt" 2>&1
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  iproute2 iperf3 ethtool bc kmod jq iputils-ping > "$out/apt-install.txt" 2>&1
{
  uname -a
  dpkg-query -W iproute2 iperf3 ethtool bc kmod jq iputils-ping
  ip -Version
  tc -Version
  iperf3 --version
  ethtool --version
  jq --version
  ping -V
  sysctl net.ipv4.tcp_available_congestion_control net.ipv4.tcp_congestion_control net.ipv4.tcp_sack net.ipv4.tcp_recovery
} > "$out/host-tools.txt" 2>&1
sudo -n modprobe sch_netem
status=0
sudo -n timeout --signal=TERM --kill-after=5s 30s unshare -nmpf --mount-proc /bin/bash > "$out/host-netem.txt" 2>&1 <<'PROBE' || status=$?
set -euxo pipefail
ip link add probe.0 type veth peer name probe.1
ip link add br0 type bridge
ip link set probe.1 master br0
ip link set probe.0 up
ip link set probe.1 up
ip link set br0 up
tc qdisc add dev probe.0 root netem limit 100 delay 50ms rate 20mbit
printf 'BASIC_NETEM_PASS\n'
seed_status=0
tc qdisc change dev probe.0 root netem limit 100 delay 50ms rate 20mbit seed 1234 || seed_status=$?
printf 'seed_exit=%s\n' "$seed_status"
tc -s -d qdisc show dev probe.0
PROBE
printf '%s\n' "$status" > "$out/host-netem-exit.txt"
(( status == 0 ))
grep -Fxq BASIC_NETEM_PASS "$out/host-netem.txt"
# These are functional smoke trials with zero random loss/jitter/duplication.
# Unsupported seeded netem is recorded, not silently used for a loss study.
seed_args=()
if grep -Fxq seed_exit=0 "$out/host-netem.txt"; then seed_args=(--seed 1234); fi
options=(--config=rbe --config=x86_64 --remote_download_outputs=toplevel)
bazel build "${options[@]}" \
  //test/benchmarks/tcp:tcp_benchmark //test/benchmarks/tcp:tcp_proxy //test/benchmarks/tcp:nsjoin \
  > "$out/build-stdout.txt" 2> "$out/build-stderr.txt"
check_status=0
bazel test "${options[@]}" --build_tag_filters= --test_tag_filters= \
  --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1 --test_output=errors \
  //test/benchmarks/tcp:tcp_proxy_nogo //tools/lint:gofmt \
  > "$out/source-checks-stdout.txt" 2> "$out/source-checks-stderr.txt" || check_status=$?
printf '%s\n' "$check_status" > "$out/source-checks-exit.txt"
(( check_status == 0 ))
bash -n test/benchmarks/tcp/tcp_benchmark.sh
# Check observations only: these short runs do not estimate CC performance.
validate_trial() (
  set -euo pipefail
  trial=$1 streams=$2 probe=$3
  for status in cleanup-exit iperf-exit receiver-exit; do
    grep -Fxq 0 "$trial/results/$status.txt"
  done
  jq -e --argjson streams "$streams" '
    (has("error") | not) and .start.test_start.num_streams == $streams and
    (.end.streams | length) == $streams and
    .end.sum_received.sender == false and
    .end.sum_received.bytes > 0 and .end.sum_received.bits_per_second > 0 and
    all(.end.streams[]; .receiver.sender == false and .receiver.bytes > 0 and .receiver.seconds > 0) and
    .end.sum_received.bytes == ([.end.streams[].receiver.bytes] | add) and
    (.intervals | length > 0) and
    all(.intervals[]; (.streams | length) == $streams)
  ' "$trial/results/receiver.json"
  grep -Eq '^BenchmarkTCP/.+ 1 [0-9.]+ Mb/s [0-9.]+ cpu-time$' "$trial/stdout.txt"
  grep -Eq 'Mbits/sec.*receiver' "$trial/results/iperf.txt"
  if [[ $probe == false ]]; then
    [[ ! -e $trial/results/ping.txt && ! -e $trial/results/ping-phases.tsv ]]
    exit 0
  fi
  grep -Eq '^[01]$' "$trial/results/ping-exit.txt"
  # Unix timestamps are adjacent to boot-time reads, not an exact conversion.
  # Require observed replies in each broad phase, without an RTT threshold.
  awk -F '\t' '
    NR == FNR {
      if (FNR == 1) { if ($0 != "phase\tboottime_seconds\tunix_seconds") exit 1; next }
      names[++n] = $1; boot[n] = $2; wall[n] = $3
      if (n > 1 && (boot[n] < boot[n-1] || wall[n] < wall[n-1])) exit 1
      next
    }
    /bytes from 10\.0\.0\.4:/ {
      split($0, part, "]"); sub(/^\[/, "", part[1]); now = part[1] + 0
      if (now >= wall[1] && now < wall[2]) before++
      if (now >= wall[2] && now < wall[3]) operation++
      if (now >= wall[3] && now <= wall[4]) drain++
    }
    END {
      if (n != 4 || names[1] != "probe-launch" || names[2] != "client-operation-begin" ||
          names[3] != "client-operation-end" || names[4] != "probe-stop" ||
          boot[2]-boot[1] < 4.9 || boot[4]-boot[3] < 1.9 ||
          before == 0 || operation == 0 || drain == 0) exit 1
      printf "reply_samples baseline=%d client_operation=%d drain=%d\n", before, operation, drain
    }
  ' "$trial/results/ping-phases.tsv" "$trial/results/ping.txt"
)
trial_status=0
while read -r name cc streams probe; do
  trial="$out/$name"
  mkdir -p "$trial"
  case "$name" in
    linux-*) flags=(--linux-client) ;;
    netstack-*) flags=(--client) ;;
    server-*) flags=(--server) ;;
    both-*) flags=(--client --server) ;;
  esac
  if [[ $probe == true ]]; then flags+=(--latency-probe); fi
  result=0
  timeout --signal=INT --kill-after=15s 90s bash -c 'bazel "$@"' _ run "${options[@]}" \
    "--execution_log_compact_file=$trial/run-execution.binpb" \
    //test/benchmarks/tcp:tcp_benchmark -- \
    "${flags[@]}" --no-user-ns --congestion-control "$cc" --ideal --latency 100 \
    --rate 20 --queue-packets 100 --duration 10 --num-client-threads "$streams" --sack \
    --disable-linux-gso --disable-linux-gro "${seed_args[@]}" --output-dir "$trial/results" \
    > "$trial/stdout.txt" 2> "$trial/stderr.txt" || result=$?
  printf '%s\n' "$result" > "$trial/exit.txt"
  if (( result != 0 )); then trial_status=1; continue; fi
  # Keep errexit active inside the check; only its enclosing process status is
  # handled as an expected observation failure so later trials still run.
  set +e
  validate_trial "$trial" "$streams" "$probe" > "$trial/observation-check.txt" 2>&1
  result=$?
  set -e
  printf '%s\n' "$result" > "$trial/observation-check-exit.txt"
  if (( result != 0 )); then trial_status=1; fi
done <<'TRIALS'
linux-reno reno 1 false
linux-cubic cubic 2 true
netstack-reno reno 1 true
netstack-cubic cubic 2 true
server-cubic cubic 2 true
both-cubic cubic 1 true
TRIALS
git diff --exit-code > "$out/source-after.diff" || trial_status=1
printf '%s\n' "$trial_status" > "$out/final-exit.txt"
exit "$trial_status"
