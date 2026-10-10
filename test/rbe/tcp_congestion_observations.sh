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
block=${QUALIFICATION_COMPARISON_BLOCK:?}
[[ $block == 1 ]]
out="$RUNNER_TEMP/qualification/tcp-delivery-owner-comparison"
mkdir -p "$out"
export out
owned=(pkg/tcpip/transport/tcp/BUILD pkg/tcpip/transport/tcp/connect.go pkg/tcpip/transport/tcp/cubic.go pkg/tcpip/transport/tcp/delivery.go pkg/tcpip/transport/tcp/endpoint.go pkg/tcpip/transport/tcp/endpoint_state.go pkg/tcpip/transport/tcp/rack.go pkg/tcpip/transport/tcp/rcv.go pkg/tcpip/transport/tcp/reno.go pkg/tcpip/transport/tcp/reno_recovery.go pkg/tcpip/transport/tcp/sack_recovery.go pkg/tcpip/transport/tcp/snd.go pkg/tcpip/transport/tcp/state.go)
finish() {
  local result=$?
  for file in "${owned[@]}"; do
    if [[ -f $out/after-${file//\//_} ]]; then
      if ! cp "$out/after-${file//\//_}" "$file" && (( result == 0 )); then
        result=1
      fi
    fi
  done
  if ! git diff --exit-code > "$out/source-after.diff" && (( result == 0 )); then
    result=1
  fi
  if ! sudo -n chown -hR -- "$(id -u):$(id -g)" "$out" && (( result == 0 )); then
    result=1
  fi
  printf '%s\n' "$result" > "$out/driver-exit.txt"
  exit "$result"
}
trap finish EXIT
[[ $(git rev-parse HEAD) == "$QUALIFICATION_COMMIT" ]]
git cat-file -p HEAD > "$out/source-commit.txt"
for file in test/benchmarks/tcp/tcp_benchmark.sh test/benchmarks/tcp/tcp_proxy.go test/benchmarks/tcp/tcp_observer.go test/benchmarks/tcp/tcp_observer_unsafe.go pkg/abi/linux/socket.go test/benchmarks/tcp/README.md test/benchmarks/tcp/BUILD tools/bazeldefs/go.bzl tools/bazeldefs/platforms.bzl .bazelrc pkg/tcpip/transport/tcp/cubic.go pkg/tcpip/transport/tcp/reno.go pkg/tcpip/transport/tcp/snd.go pkg/tcpip/transport/tcp/connect.go pkg/tcpip/transport/tcp/endpoint.go pkg/tcpip/transport/tcp/state.go pkg/tcpip/stack/stack.go pkg/tcpip/stack/transport_demuxer.go test/rbe/actions.sh .github/workflows/build.yml test/rbe/tcp_congestion_observations.sh pkg/tcpip/transport/tcp/segment.go pkg/tcpip/transport/tcp/endpoint_state.go pkg/tcpip/transport/tcp/sack_scoreboard.go pkg/tcpip/transport/tcp/rack.go pkg/tcpip/transport/tcp/BUILD pkg/tcpip/transport/tcp/delivery.go pkg/tcpip/transport/tcp/rcv.go pkg/tcpip/transport/tcp/reno_recovery.go pkg/tcpip/transport/tcp/sack_recovery.go; do
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
# These shared-bottleneck trials configure no random loss/jitter/duplication.
# Unsupported seeded netem is recorded, not silently used for a loss study.
seed_args=()
if grep -Fxq seed_exit=0 "$out/host-netem.txt"; then seed_args=(--seed 1234); fi
options=(--config=rbe --config=x86_64 --remote_download_outputs=toplevel)
before=d4bf42718b227ed549d7c8d9df5e361ef7b2c287
if ! git cat-file -e "$before^{commit}" 2>/dev/null; then
  timeout --signal=TERM --kill-after=5s 60s git fetch --no-tags --depth=1 origin "$before" \
    > "$out/before-fetch.txt" 2>&1
fi
git cat-file -p "$before" > "$out/before-commit.txt"
for file in "${owned[@]}"; do
  if [[ $file == pkg/tcpip/transport/tcp/delivery.go ]]; then
    [[ -z $(git ls-tree "$before" -- "$file") ]]
    printf 'absent\n' > "$out/before-${file//\//_}.absent"
  else
    git show "$before:$file" > "$out/before-${file//\//_}"
  fi
  cp "$file" "$out/after-${file//\//_}"
done
printf '%s\n' \
  "e5cebfb235980998f4b78819a4a65abff26609e33c78f1f7fd156d6d939451fe  $out/before-pkg_tcpip_transport_tcp_BUILD" \
  "3e962d788415a9a5721e07c4a6fa4a787775b87a869de90085cf8e30535198a6  $out/before-pkg_tcpip_transport_tcp_connect.go" \
  "a4ebb036cb81ee238010191adf076d55ea44781ffb32641873d5e14d840ebae6  $out/before-pkg_tcpip_transport_tcp_cubic.go" \
  "cfceed6d56db69d60cc62baeb565edc1cd2f28e15f4c88aa6d398ef1c8333826  $out/before-pkg_tcpip_transport_tcp_endpoint.go" \
  "6df44c2d0841484499cb9aa95c74233b658ca4fbf26ae6703bd3092696a4bb7d  $out/before-pkg_tcpip_transport_tcp_endpoint_state.go" \
  "9572413a9b8a69a0641f01893a76514764fd5f0b5bf5d40f7a9f9bcd80d3687f  $out/before-pkg_tcpip_transport_tcp_rack.go" \
  "4d78e88c63d326bbf9bb9a87aaae67e49e74575dff39e4e97cafd6bb94aed56a  $out/before-pkg_tcpip_transport_tcp_rcv.go" \
  "d8773d8cccb9c0512dabf3271ba21dd7da61253f5536e6445314118695148d6d  $out/before-pkg_tcpip_transport_tcp_reno.go" \
  "d3efb50ba458432a0926cd775221e4b42b35b0906444d454d1d775f8979f8a77  $out/before-pkg_tcpip_transport_tcp_reno_recovery.go" \
  "07bea5b3795bd7518a19fb57838179bf9c49e9483bbf30efd66395ef89865e24  $out/before-pkg_tcpip_transport_tcp_sack_recovery.go" \
  "3f74f4d4a90805d45de65efb5cc7543d91c95627c0ca3d3d598cf112565d0fa9  $out/before-pkg_tcpip_transport_tcp_snd.go" \
  "499f2a0181b74748e0afa39b52f5aa24137a29cd2311db95e4cf793d5aacce7f  $out/before-pkg_tcpip_transport_tcp_state.go" \
  "7c0f10462e366342666ab04357d1d9d4f97e1f23d86a5e61588f7c68079cd24f  $out/after-pkg_tcpip_transport_tcp_BUILD" \
  "990dd527319ef430f98eb4d1a73f00eeb7070861ebb0d3e41b33fadf7c3f1217  $out/after-pkg_tcpip_transport_tcp_connect.go" \
  "d06aed73d32051142811fea84faa3979e015c75a5009030dcf31f3ac047771da  $out/after-pkg_tcpip_transport_tcp_cubic.go" \
  "3cc88bc690cfab12bd8c72ac2b7be122158d923fade03a751530f5e31244d6c9  $out/after-pkg_tcpip_transport_tcp_delivery.go" \
  "c08fb042b00e77b83c68aebc4afd46dbb12dfc2cdb8e43bb13f5ca6d740e14a7  $out/after-pkg_tcpip_transport_tcp_endpoint.go" \
  "f91ab8d996f558a3fd569c6e70897e098615ac664fe26259fa2e8152b0592af5  $out/after-pkg_tcpip_transport_tcp_endpoint_state.go" \
  "28cb789525e89e77b62129f7bde90cea518aa01419ee3d23a519f68dd09b96c2  $out/after-pkg_tcpip_transport_tcp_rack.go" \
  "9cd7728ceb3f672c6df8b21a69400f739576ffa78aa3ea58ed7ee64f1f82f40b  $out/after-pkg_tcpip_transport_tcp_rcv.go" \
  "4dea1df2a72ca4fe8d27f7b443a9603d9903cb1a5b9e1791ed1d102727c7a4b6  $out/after-pkg_tcpip_transport_tcp_reno.go" \
  "3cac44744ad0a40a3b3f8ef2ac93555f916542097d83bd66b0f2db3c9bb5ac4c  $out/after-pkg_tcpip_transport_tcp_reno_recovery.go" \
  "863b27ef73a30678b6da1b380303ae8b0349b8775f5784fe25f6f3d34f419af6  $out/after-pkg_tcpip_transport_tcp_sack_recovery.go" \
  "99c5c91248fe0f98adcb55edc75f19ca90f76b29503dec1c807553f44214c81c  $out/after-pkg_tcpip_transport_tcp_snd.go" \
  "bd8c528b5dfc92cf540ed98f21e51d9e2a0e1a0be3366f7afc654b1332c7cb84  $out/after-pkg_tcpip_transport_tcp_state.go" | sha256sum --check --strict
activate_variant() {
  local variant=$1 file
  for file in "${owned[@]}"; do
    if [[ -f $out/$variant-${file//\//_}.absent ]]; then
      [[ $variant == before && $file == pkg/tcpip/transport/tcp/delivery.go ]]
      rm -f -- "$file"
    else
      cp "$out/$variant-${file//\//_}" "$file"
    fi
  done
}

for variant in before after; do
  activate_variant "$variant"
  directory="$out/prebuild-$variant"
  mkdir -p "$directory/helpers"
  for file in "${owned[@]}"; do
    if [[ -f $file ]]; then
      cp "$file" "$directory/${file//\//_}.source"
    else
      cp "$out/$variant-${file//\//_}.absent" "$directory/${file//\//_}.absent"
    fi
  done
  git diff -- "${owned[@]}" > "$directory/variant.patch"
  bazel build "${options[@]}" \
    "--execution_log_compact_file=$directory/build.binpb" \
    //test/benchmarks/tcp:tcp_benchmark //test/benchmarks/tcp:tcp_proxy //test/benchmarks/tcp:nsjoin \
    > "$directory/build-stdout.txt" 2> "$directory/build-stderr.txt"
  for helper in tcp_proxy nsjoin; do
    output=$(bazel cquery "${options[@]}" --output=files "//test/benchmarks/tcp:$helper" \
      2> "$directory/$helper-query-stderr.txt")
    printf '%s\n' "$output" > "$directory/$helper-output.txt"
    [[ $output == bazel-out/*/bin/test/benchmarks/tcp/$helper ]]
    sudo -n install -m 755 -o "$(id -u)" -g "$(id -g)" -- \
      "$output" "$directory/helpers/$helper"
  done
  sha256sum "$directory"/helpers/* > "$directory/helper-hashes.txt"
done
activate_variant after
git diff --exit-code > "$out/source-restored.diff"
# Reprime the restored source before traffic; every measured run retains its
# compact trace so fresh measured compiler work remains a result failure.
bazel build "${options[@]}" //test/benchmarks/tcp:tcp_benchmark \
  > "$out/reprime-stdout.txt" 2> "$out/reprime-stderr.txt"
# The harness and periodic recorder are identical in both arms. Controller
# policy is unchanged; its accesses use the new owner in the after arm.
bash -n test/benchmarks/tcp/tcp_benchmark.sh
# Preserve complete reports, including zero-byte flows. Fairness is reduced
# offline using conservative common-window interval bounds, not session means.
validate_receiver() {
  local dir=$1 streams=$2
  for status in cleanup-exit iperf-exit receiver-exit flow-exit; do
    grep -Fxq 0 "$dir/$status.txt"
  done
  jq -e --argjson streams "$streams" '
    (has("error") | not) and .start.test_start.num_streams == $streams and
    (.end.streams | length) == $streams and
    .end.sum_received.sender == false and .end.sum_received.bytes >= 0 and
    .end.sum_received.bits_per_second >= 0 and
    all(.end.streams[]; .receiver.sender == false and .receiver.bytes >= 0 and .receiver.seconds > 0) and
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
          names[4] != "probe-stop" || boot[2]-boot[1] < 4.9 || boot[4]-boot[3] < 1.9) exit 1
      printf "reply_samples baseline=%d operation=%d drain=%d\n", before, operation, drain
    }
  ' "$dir/ping-phases.tsv" "$dir/ping.txt"
}
validate_json() {
  local file=$1 mode=$2 role=$3 cc=$4 backend=$5
  jq -e --arg mode "$mode" --arg role "$role" --arg cc "$cc" --arg backend "$backend" '
    .Mode == $mode and .Role == $role and .ConfiguredCongestionControl == $cc and
    .Limit == 12000 and .Truncated == false and .Error == "" and
    .Unavailable >= 0 and .IntervalNS == (if $mode == "periodic" then 100000000 else 0 end) and
    (.Records | length > 0 and length <= 12000) and
    all(.Records[];
      (.Local | type == "string") and (.Remote | type == "string") and
      .BootBeginNS > 0 and .BootEndNS >= .BootBeginNS and .UnixNS > 0 and
      (if $mode == "packet" then .BootEndNS == .BootBeginNS else true end) and
      if $backend == "Netstack" then
        (has("Native") | not) and (.Netstack | type == "object") and
        (.Netstack.StackTimeNS | test("^[0-9]+$")) and
        (.Netstack.Cwnd | type == "number") and
        (.Netstack.Outstanding | type == "number") and
        (.Netstack.SendWindowBytes | type == "number") and
        (.Netstack.SendBufferUsed | type == "number") and
        (.Netstack.RTT | type == "object") and (.Netstack.Recovery | type == "object")
      else
        (has("Netstack") | not) and (.Native | type == "object") and
        .Native.InfoBytes >= 104 and .Native.CongestionControl == $cc and
        .Native.RTT >= 0 and .Native.RTTVar >= 0 and .Native.RTO >= 0 and
        all(.Native | .NotSentBytes, .BytesAcked, .DeliveryRate, .BusyTime, .ReceiveWindowLimited, .SendBufferLimited;
          . == null or (type == "number" and . >= 0))
      end) and
    any(.Records[]; .[$backend].Cwnd > 0 and .[$backend].MSS > 0)
  ' "$file" > /dev/null
}
validate_shared() (
  set -euo pipefail
  local trial=$1 delay=$2 first_cc=$3 second_cc=$4 first_stack=$5 second_stack=$6 dir=$1/results
  local first_backend=Native second_backend=Native
  if [[ $first_stack == netstack ]]; then first_backend=Netstack; fi
  if [[ $second_stack == netstack ]]; then second_backend=Netstack; fi
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
  validate_json "$dir/primary/tcp-observations.json" periodic client "$first_cc" "$first_backend"
  validate_json "$dir/secondary/tcp-observations.json" periodic client "$second_cc" "$second_backend"
  grep -Fq 'proxy connection: incoming remote=' "$dir/primary/stderr.txt"
  grep -Fq 'proxy connection: incoming remote=' "$dir/secondary/stderr.txt"
  # Bind each receiver stream through both proxy tuples to its actual WAN
  # observation during saved-result reduction; do not guess from cwnd.
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
# Keep the declared trial order and actual orientation in each block.
scenarios=(
  'reno-netstack-first netstack reno linux reno 0'
  'reno-linux-first linux reno netstack reno 0'
  'cubic-netstack-first netstack cubic linux cubic 0'
  'cubic-linux-first linux cubic netstack cubic 0'
)
order=(0 2 3 1)
printf 'block=%s order=%s\n' "$block" "${order[*]}" > "$out/block.txt"
printf 'position\tcase\tvariant\tprimary_stack\tprimary_cc\tsecondary_stack\tsecondary_cc\tdelay\n' > "$out/trials.tsv"
trial_status=0
position=0
for index in "${order[@]}"; do
  read -r name first_stack first_cc second_stack second_cc delay <<< "${scenarios[index]}"
  position=$((position + 1))
  variants=(before after)
  if (( (position + block) % 2 != 0 )); then variants=(after before); fi
  for variant in "${variants[@]}"; do
    trial="$out/$name-$variant"
    mkdir -p "$trial"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$position" "$name" "$variant" "$first_stack" "$first_cc" "$second_stack" "$second_cc" "$delay" >> "$out/trials.tsv"
    printf 'block=%s position=%s scenario=%s variant=%s primary=%s/%s secondary=%s/%s delay=%s\n' \
      "$block" "$position" "$name" "$variant" "$first_stack" "$first_cc" "$second_stack" "$second_cc" "$delay" > "$trial/identity.txt"
    flags=(--linux-client)
    if [[ $first_stack == netstack ]]; then flags=(--client); fi
    result=0
    timeout --signal=INT --kill-after=15s 180s bash -c 'bazel "$@"' _ run "${options[@]}" \
      "--execution_log_compact_file=$trial/run-execution.binpb" \
      //test/benchmarks/tcp:tcp_benchmark -- \
      "${flags[@]}" --no-user-ns --congestion-control "$first_cc" \
      --second-client "$second_stack" --second-congestion-control "$second_cc" \
      --second-start-delay "$delay" --tcp-observations --latency-probe --output-dir "$trial/results" \
      --helpers "$out/prebuild-$variant/helpers" \
      --ideal --mtu 1500 --latency 100 --rate 20 --queue-packets 100 \
      --duration 120 --num-client-threads 1 --sack \
      --disable-linux-gso --disable-linux-gro "${seed_args[@]}" \
      > "$trial/stdout.txt" 2> "$trial/stderr.txt" || result=$?
    printf '%s\n' "$result" > "$trial/exit.txt"
    if (( result != 0 )); then trial_status=1; continue; fi
    set +e
    validate_shared "$trial" "$delay" "$first_cc" "$second_cc" "$first_stack" "$second_stack" > "$trial/observation-check.txt" 2>&1
    result=$?
    set -e
    printf '%s\n' "$result" > "$trial/observation-check-exit.txt"
    if (( result != 0 )); then trial_status=1; fi
  done
done
git diff --exit-code > "$out/source-after.diff" || trial_status=1
printf '%s\n' "$trial_status" > "$out/final-exit.txt"
exit "$trial_status"
