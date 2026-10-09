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
out="$RUNNER_TEMP/qualification/tcp-congestion-sharing"
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
# Prior Go/Nogo checks apply unchanged; this run checks the shell topology and lifecycle.
bash -n test/benchmarks/tcp/tcp_benchmark.sh
# Only functional evidence: no goodput comparison or fairness calculation.
validate_receiver() {
  local dir=$1 streams=$2
  for status in cleanup-exit iperf-exit receiver-exit flow-exit; do
    grep -Fxq 0 "$dir/$status.txt"
  done
  jq -e --argjson streams "$streams" '
    (has("error") | not) and .start.test_start.num_streams == $streams and
    (.end.streams | length) == $streams and
    .end.sum_received.sender == false and .end.sum_received.bytes > 0 and
    .end.sum_received.bits_per_second > 0 and
    all(.end.streams[]; .receiver.sender == false and .receiver.bytes > 0 and .receiver.seconds > 0) and
    .end.sum_received.bytes == ([.end.streams[].receiver.bytes] | add) and
    (.intervals | length > 0) and all(.intervals[]; (.streams | length) == $streams)
  ' "$dir/receiver.json"
  grep -Eq 'Mbits/sec.*receiver' "$dir/iperf.txt"
}
validate_probe() {
  local dir=$1 begin=$2 end=$3
  grep -Eq '^[01]$' "$dir/ping-exit.txt"
  awk -F '\t' -v begin="$begin" -v end="$end" '
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
      if (n != 4 || names[1] != "probe-launch" || names[2] != begin || names[3] != end ||
          names[4] != "probe-stop" || boot[2]-boot[1] < 4.9 || boot[4]-boot[3] < 1.9 ||
          before == 0 || operation == 0 || drain == 0) exit 1
      printf "reply_samples baseline=%d operation=%d drain=%d\n", before, operation, drain
    }
  ' "$dir/ping-phases.tsv" "$dir/ping.txt"
}
validate_single() (
  set -euo pipefail
  local trial=$1 streams=$2 probe=$3
  validate_receiver "$trial/results" "$streams"
  grep -Fxq 0 "$trial/results/topology-cleanup-exit.txt"
  grep -Eq '^BenchmarkTCP/.+ 1 [0-9.]+ Mb/s [0-9.]+ cpu-time$' "$trial/stdout.txt"
  if [[ $probe == true ]]; then
    validate_probe "$trial/results" client-operation-begin client-operation-end
  else
    [[ ! -e $trial/results/ping.txt ]]
  fi
)
validate_shared() (
  set -euo pipefail
  local trial=$1 delay=$2 dir=$1/results
  grep -Fxq 0 "$dir/topology-cleanup-exit.txt"
  for flow in primary secondary; do
    validate_receiver "$dir/$flow" 1
    grep -Eq "^BenchmarkTCP/flow=$flow/role=client/stack=(linux|netstack)/cc=(reno|cubic)/.+ 1 [0-9.]+ Mb/s [0-9.]+ cpu-time$" "$dir/$flow/benchmark.txt"
    awk -F '\t' '
      NR == 1 { if ($0 != "phase\tboottime_seconds\tunix_seconds") exit 1; next }
      { names[++n]=$1; times[n]=$2; if (n>1 && times[n]<times[n-1]) exit 1 }
      END { if (n!=3 || names[1]!="proxy-start" || names[2]!="client-operation-begin" || names[3]!="client-operation-end") exit 1 }
    ' "$dir/$flow/flow-phases.tsv"
  done
  # A receive window is enclosed by that flow's client-operation markers.
  # These are conservative bounds, not a point alignment of the two reports.
  first_duration=$(jq -er '.end.sum_received.seconds' "$dir/primary/receiver.json")
  second_duration=$(jq -er '.end.sum_received.seconds' "$dir/secondary/receiver.json")
  awk -F '\t' -v delay="$delay" -v first_duration="$first_duration" -v second_duration="$second_duration" '
    FNR==1 { file++; next }
    { times[file,$1]=$2 }
    END {
      observed=times[2,"proxy-start"]-times[1,"proxy-start"]
      if (delay>0 && observed<delay-0.1) exit 1
      b1=times[1,"client-operation-begin"]; e1=times[1,"client-operation-end"]
      b2=times[2,"client-operation-begin"]; e2=times[2,"client-operation-end"]
      if (e1-b1<first_duration || e2-b2<second_duration) exit 1
      latest_start=(e1-first_duration>e2-second_duration ? e1-first_duration : e2-second_duration)
      earliest_end=(b1+first_duration<b2+second_duration ? b1+first_duration : b2+second_duration)
      # Reserve one second for timestamp granularity and scheduling margins.
      overlap=earliest_end-latest_start-1
      if (overlap<=0) exit 1
      printf "observed_start_delay=%f conservative_receive_window_overlap=%f\n", observed, overlap
    }
  ' "$dir/primary/flow-phases.tsv" "$dir/secondary/flow-phases.tsv"
  [[ $(grep -c '^qdisc netem ' "$dir/qdisc-after.txt") == 2 ]]
  grep -Fq client2.0 "$dir/interface-features.txt"
  validate_probe "$dir" flows-begin flows-end
)
validate_term() (
  set -euo pipefail
  local dir=$1/results
  grep -Fxq 0 "$dir/topology-cleanup-exit.txt"
  for flow in primary secondary; do
    grep -Fxq 143 "$dir/$flow/flow-exit.txt"
    grep -Fxq 0 "$dir/$flow/cleanup-exit.txt"
    grep -Fxq 143 "$dir/$flow/iperf-exit.txt"
    [[ -s $dir/$flow/receiver-exit.txt ]]
    grep -Fq client-operation-begin "$dir/$flow/flow-phases.tsv"
  done
  [[ ! -e $dir/ping.txt ]]
)
trial_status=0
while read -r name cc streams probe second delay; do
  trial="$out/$name"
  mkdir -p "$trial"
  run_options=()
  flags=(--linux-client)
  case "$name" in
    netstack-*|both-*|term-*) flags=(--client) ;;
    server-*) flags=(--server) ;;
    ipv6-*) flags=(--client --ipv6) ;;
  esac
  if [[ $name == both-cubic ]]; then flags=(--client --server); fi
  duration=10
  if [[ $second != none ]]; then
    duration=12
    second_stack=${second%%/*}
    second_cc=${second#*/}
    flags+=(--second-client "$second_stack" --second-start-delay "$delay")
    # The native same-CC control exercises the documented default.
    if [[ $name != linux-reno-shared ]]; then flags+=(--second-congestion-control "$second_cc"); fi
  fi
  if [[ $probe == true ]]; then flags+=(--latency-probe); fi
  if [[ $name == legacy-no-output ]]; then
    duration=2
  else
    flags+=(--output-dir "$trial/results")
  fi
  expected=0
  if [[ $name == term-mixed ]]; then
    duration=30
    expected=124
    run_options+=(--run_under='timeout --signal=TERM --kill-after=15s 12s')
  fi
  result=0
  timeout --signal=INT --kill-after=15s 90s bash -c 'bazel "$@"' _ run "${options[@]}" \
    "--execution_log_compact_file=$trial/run-execution.binpb" "${run_options[@]}" \
    //test/benchmarks/tcp:tcp_benchmark -- \
    "${flags[@]}" --no-user-ns --congestion-control "$cc" --ideal --latency 100 \
    --rate 20 --queue-packets 100 --duration "$duration" --num-client-threads "$streams" --sack \
    --disable-linux-gso --disable-linux-gro "${seed_args[@]}" \
    > "$trial/stdout.txt" 2> "$trial/stderr.txt" || result=$?
  printf '%s\n' "$result" > "$trial/exit.txt"
  if (( result != expected )); then trial_status=1; continue; fi
  set +e
  if [[ $name == term-mixed ]]; then
    validate_term "$trial" > "$trial/observation-check.txt" 2>&1
    result=$?
  elif [[ $name == legacy-no-output ]]; then
    grep -Eq '^BenchmarkTCP/role=client/stack=linux/cc=reno/.+ 1 [0-9.]+ Mb/s [0-9.]+ cpu-time$' "$trial/stdout.txt"
    result=$?
  elif [[ $second != none ]]; then
    validate_shared "$trial" "$delay" > "$trial/observation-check.txt" 2>&1
    result=$?
  else
    validate_single "$trial" "$streams" "$probe" > "$trial/observation-check.txt" 2>&1
    result=$?
  fi
  set -e
  printf '%s\n' "$result" > "$trial/observation-check-exit.txt"
  if (( result != 0 )); then trial_status=1; fi
done <<'TRIALS'
legacy-no-output reno 1 false none 0
linux-reno reno 1 false none 0
linux-cubic cubic 2 true none 0
netstack-reno reno 1 true none 0
netstack-cubic cubic 2 true none 0
server-cubic cubic 2 true none 0
both-cubic cubic 1 true none 0
ipv6-netstack-cubic cubic 1 false none 0
linux-reno-shared reno 1 true linux/reno 0
netstack-cubic-shared cubic 1 true netstack/cubic 0
netstack-cubic-native-reno cubic 1 true linux/reno 0
linux-reno-netstack-cubic-late reno 1 true netstack/cubic 3
netstack-cubic-reno-late cubic 1 true netstack/reno 3
term-mixed cubic 1 false linux/reno 0
TRIALS
git diff --exit-code > "$out/source-after.diff" || trial_status=1
printf '%s\n' "$trial_status" > "$out/final-exit.txt"
exit "$trial_status"
