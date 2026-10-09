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
out="$RUNNER_TEMP/qualification/tcp-congestion-wan-observations"
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
for file in test/benchmarks/tcp/tcp_benchmark.sh test/benchmarks/tcp/tcp_proxy.go test/benchmarks/tcp/tcp_observer.go test/benchmarks/tcp/README.md test/benchmarks/tcp/BUILD tools/bazeldefs/go.bzl tools/bazeldefs/platforms.bzl .bazelrc pkg/tcpip/transport/tcp/cubic.go pkg/tcpip/transport/tcp/reno.go pkg/tcpip/transport/tcp/snd.go pkg/tcpip/transport/tcp/connect.go pkg/tcpip/transport/tcp/endpoint.go pkg/tcpip/transport/tcp/state.go pkg/tcpip/stack/stack.go pkg/tcpip/stack/transport_demuxer.go test/rbe/actions.sh .github/workflows/build.yml test/rbe/tcp_congestion_observations.sh; do
  cp "$file" "$out/source-${file//\//_}"
done
sudo -n apt-get update > "$out/apt-update.txt" 2>&1
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  iproute2 iperf3 ethtool bc kmod jq iputils-ping > "$out/apt-install.txt" 2>&1
{
  uname -a
  dpkg-query -W iproute2 iperf3 ethtool bc kmod jq iputils-ping
  ip -Version
  ss -V
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
for variant in normal race; do
  extra=()
  if [[ $variant == race ]]; then extra+=(--config=race); fi
  bazel build "${options[@]}" "${extra[@]}" \
    "--execution_log_compact_file=$out/build-$variant.binpb" \
    //test/benchmarks/tcp:tcp_benchmark //test/benchmarks/tcp:tcp_proxy //test/benchmarks/tcp:nsjoin \
    > "$out/build-$variant-stdout.txt" 2> "$out/build-$variant-stderr.txt"
done
bazel test "${options[@]}" --keep_going --build_tag_filters= --test_tag_filters= \
  --test_output=errors --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1 \
  "--execution_log_compact_file=$out/checks.binpb" \
  //test/benchmarks/tcp:tcp_proxy_nogo //tools/lint:gofmt //tools/lint:buildifier \
  > "$out/checks-stdout.txt" 2> "$out/checks-stderr.txt"
bash -n test/benchmarks/tcp/tcp_benchmark.sh
validate_receiver() {
  local dir=$1 streams=$2
  for status in iperf-exit receiver-exit; do
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
validate_json() {
  local file=$1 mode=$2 role=$3 cc=$4
  jq -e --arg mode "$mode" --arg role "$role" --arg cc "$cc" '
    .Mode == $mode and .Role == $role and .ConfiguredCongestionControl == $cc and
    .Limit == 12000 and .Truncated == false and .Error == "" and
    .Unavailable >= 0 and .IntervalNS == (if $mode == "periodic" then 100000000 else 0 end) and
    (.Records | length > 0 and length <= 12000) and
    all(.Records[];
      (.Local | type == "string") and (.Remote | type == "string") and
      (.StackTimeNS | test("^[0-9]+$")) and
      .BootBeginNS > 0 and .BootEndNS >= .BootBeginNS and .UnixNS > 0 and
      (if $mode == "packet" then .BootEndNS == .BootBeginNS else true end) and
      (.Cwnd | type == "number") and (.Outstanding | type == "number") and
      (.SendWindowBytes | type == "number") and (.SendBufferUsed | type == "number") and
      (.RTT | type == "object") and (.Recovery | type == "object")) and
    any(.Records[]; .Cwnd > 0 and .MSS > 0)
  ' "$file" > /dev/null
}
validate_native() {
  local dir=$1 cc=$2
  grep -Fxq 0 "$dir/tcp-native-exit.txt"
  grep -Fxq SS_CAPTURE_STOPPED "$dir/tcp-native.txt"
  ! grep -Fq SS_CAPTURE_LIMIT_REACHED "$dir/tcp-native.txt"
  grep -Eq "^[[:space:]]+${cc}[[:space:]]" "$dir/tcp-native.txt"
  grep -Fq '10.0.0.3:44001' "$dir/tcp-native.txt"
  awk -F '\t' '
    $1=="SS_SAMPLE_BEGIN" {
      if (active || $2!=count || $3<=0 || $4<=0) exit 1
      active=1; start=$3; wall=$4; next
    }
    $1=="SS_SAMPLE_END" {
      if (!active || $2!=count || $3<start || $4<wall || $5!=0) exit 1
      active=0; count++; next
    }
    END { if (active || count<2) exit 1; print "completed_native_samples="count }
  ' "$dir/tcp-native.txt"
}
validate_trial() (
  set -euo pipefail
  local trial=$1 name=$2 cc=$3 dir=$1/results
  grep -Fxq 0 "$dir/topology-cleanup-exit.txt"
  if [[ $name == term ]]; then
    for flow in primary secondary; do
      grep -Fxq 143 "$dir/$flow/flow-exit.txt"
      grep -Fxq 143 "$dir/$flow/iperf-exit.txt"
      grep -Fxq 0 "$dir/$flow/cleanup-exit.txt"
      [[ -s $dir/$flow/receiver-exit.txt ]]
      grep -Fq client-operation-begin "$dir/$flow/flow-phases.tsv"
    done
  elif [[ $name == packet-write-error ]]; then
    validate_receiver "$dir" 1
    grep -Fxq 1 "$dir/cleanup-exit.txt"
    grep -Fxq 1 "$dir/flow-exit.txt"
    grep -Eq 'TCP capture:.*(no space left on device|file system full)' "$trial/stderr.txt"
    grep -Eq '^BenchmarkTCP/.+ 1 [0-9.]+ Mb/s [0-9.]+ cpu-time$' "$trial/stdout.txt"
    exit 0
  elif [[ $name == packet-both ]]; then
    validate_receiver "$dir" 1
    grep -Fxq 0 "$dir/cleanup-exit.txt"
    grep -Fxq 0 "$dir/flow-exit.txt"
    validate_json "$trial/client-packet.json" packet client "$cc"
    validate_json "$trial/server-packet.json" packet server "$cc"
    grep -Fq 'proxy connection: incoming remote=' "$trial/stderr.txt"
    exit 0
  else
    for flow in primary secondary; do
      validate_receiver "$dir/$flow" 1
      grep -Fxq 0 "$dir/$flow/cleanup-exit.txt"
      grep -Fxq 0 "$dir/$flow/flow-exit.txt"
    done
  fi
  if [[ $name == disabled ]]; then
    for flow in primary secondary; do
      [[ ! -e $dir/$flow/tcp-netstack.json && ! -e $dir/$flow/tcp-native.txt ]]
    done
  else
    validate_json "$dir/primary/tcp-netstack.json" periodic client "$cc"
    validate_native "$dir/secondary" "$cc"
    grep -Fq 'proxy connection: incoming remote=' "$dir/primary/stderr.txt"
    grep -Fq 'proxy connection: incoming remote=' "$dir/secondary/stderr.txt"
    # Exact receiver stream -> both proxy tuples -> WAN observation matching
    # is a saved-result obligation; do not select a socket by largest cwnd.
  fi
)
trial_status=0
while read -r name cc duration; do
  trial="$out/$name"
  mkdir -p "$trial"
  run_options=()
  flags=(--client --second-client linux --second-congestion-control "$cc")
  expected=0
  case "$name" in
    disabled) ;;
    packet-both)
      flags=(--client --server --client_tcp_probe_file "$trial/client-packet.json" --server_tcp_probe_file "$trial/server-packet.json") ;;
    packet-write-error)
      flags=(--client --client_tcp_probe_file /dev/full)
      expected=1 ;;
    *) flags+=(--tcp-observations) ;;
  esac
  if [[ $name == periodic-race ]]; then run_options+=(--config=race); fi
  if [[ $name == term ]]; then
    expected=124
    run_options+=(--run_under='timeout --signal=TERM --kill-after=15s 12s')
  fi
  result=0
  timeout --signal=INT --kill-after=15s 90s bash -c 'bazel "$@"' _ run "${options[@]}" \
    "--execution_log_compact_file=$trial/run-execution.binpb" "${run_options[@]}" \
    //test/benchmarks/tcp:tcp_benchmark -- \
    "${flags[@]}" --output-dir "$trial/results" --no-user-ns --congestion-control "$cc" \
    --ideal --latency 100 --rate 20 --queue-packets 100 --duration "$duration" \
    --num-client-threads 1 --sack --disable-linux-gso --disable-linux-gro "${seed_args[@]}" \
    > "$trial/stdout.txt" 2> "$trial/stderr.txt" || result=$?
  printf '%s\n' "$result" > "$trial/exit.txt"
  if (( result != expected )); then trial_status=1; continue; fi
  set +e
  validate_trial "$trial" "$name" "$cc" > "$trial/observation-check.txt" 2>&1
  result=$?
  set -e
  printf '%s\n' "$result" > "$trial/observation-check-exit.txt"
  if (( result != 0 )); then trial_status=1; fi
done <<'TRIALS'
disabled reno 5
periodic-reno reno 10
periodic-cubic cubic 10
periodic-race reno 5
packet-both reno 2
packet-write-error reno 1
term reno 30
TRIALS
git diff --exit-code > "$out/source-after.diff" || trial_status=1
printf '%s\n' "$trial_status" > "$out/final-exit.txt"
exit "$trial_status"
