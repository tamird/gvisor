#!/bin/bash

# Copyright 2018 The gVisor Authors.
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

# TCP benchmark; see README.md for documentation.
set -euo pipefail

# Fixed parameters.
command_line=("$0" "$@")
iperf_port=45201 # Not likely to be privileged.
proxy_port=44000 # Ditto.
mask=8
client_addr=10.0.0.1
client_proxy_addr=10.0.0.2
server_proxy_addr=10.0.0.3
server_addr=10.0.0.4
full_server_addr=${server_addr}:${iperf_port}
full_server_proxy_addr=${server_proxy_addr}:${proxy_port}
iperf_binary_name=iperf3
iperf_version_arg=

# Defaults; this provides a reasonable approximation of a decent internet link.
# Parameters can be varied independently from this set to see response to
# various changes in the kind of link available.
client=false
server=false
linux_client=false
verbose=false
gso=0
swgso=false
mtu=1280                # 1280 is a reasonable lowest-common-denominator.
latency=10              # 10ms approximates a fast, dedicated connection.
latency_variation=1     # +/- 1ms is a relatively low amount of jitter.
loss=0.1                # 0.1% loss is non-zero, but not extremely high.
duplicate=0.1           # 0.1% means duplicates are 1/10x as frequent as losses.
duration=30             # 30s is enough time to consistent results (experimentally).
congestion_control=reno # Select both stacks explicitly instead of inheriting Linux's default.
rate_mbps=             # Empty leaves the link rate unlimited.
queue_packets=1000     # The default netem queue limit, made explicit.
seed=                  # Requires a tc/kernel combination that supports netem seed.
output_dir=
latency_probe=false
second_client=
second_congestion_control=
second_start_delay=0
helper_dir="$(dirname "$0")"
netstack_opts=
disable_linux_gso=
disable_linux_gro=
gro=false
num_client_threads=1
sniff=false
xdp=false
declare -a unshare_opts=( -U -r )

# Check for netem support.
if ! lsmod | grep sch_netem >/dev/null; then
  echo "warning: sch_netem may not be installed." >&2
fi

function checktmp() {
  if [[ "$1" == /tmp || "$1" == /tmp/* ]]; then
    echo "Don't use /tmp for output files ('$1') -- tcp_benchmark mounts over /tmp and your file will never make it to the root /tmp."
    return 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client)
      client=true
      ;;
    --second-client)
      shift
      second_client=$1
      ;;
    --second-congestion-control)
      shift
      second_congestion_control=$1
      ;;
    --second-start-delay)
      shift
      second_start_delay=$1
      ;;
    --linux-client)
      linux_client=true
      ;;
    --client_tcp_probe_file)
      shift
      netstack_opts="${netstack_opts} -client_tcp_probe_file=$1"
      ;;
    --server)
      server=true
      ;;
    --verbose)
      verbose=true
      ;;
    --gso)
      shift
      gso=$1
      ;;
    --swgso)
      swgso=true
      ;;
    --server_tcp_probe_file)
      shift
      netstack_opts="${netstack_opts} -server_tcp_probe_file=$1"
      ;;
    --ideal)
      mtu=1500            # Standard ethernet.
      latency=0           # No latency.
      latency_variation=0 # No jitter.
      loss=0              # No loss.
      duplicate=0         # No duplicates.
      ;;
    --mtu)
      shift
      [[ "$#" -le 0 ]] && echo "no mtu provided" && exit 1
      mtu=$1
      ;;
    --sack)
      netstack_opts="${netstack_opts} -sack"
      ;;
    --rack)
      netstack_opts="${netstack_opts} -rack"
      ;;
    --cubic)
      congestion_control=cubic
      ;;
    --congestion-control)
      shift
      [[ "$#" -le 0 ]] && echo "no congestion control provided" && exit 1
      congestion_control=$1
      ;;
    --rate)
      shift
      [[ "$#" -le 0 ]] && echo "no rate provided" && exit 1
      rate_mbps=$1
      ;;
    --queue-packets)
      shift
      [[ "$#" -le 0 ]] && echo "no queue limit provided" && exit 1
      queue_packets=$1
      ;;
    --seed)
      shift
      [[ "$#" -le 0 ]] && echo "no seed provided" && exit 1
      seed=$1
      ;;
    --output-dir)
      shift
      [[ "$#" -le 0 ]] && echo "no output directory provided" && exit 1
      output_dir=$1
      ;;
    --moderate-recv-buf)
      netstack_opts="${netstack_opts} -moderate_recv_buf"
      ;;
    --latency-probe)
      latency_probe=true
      ;;
    --duration)
      shift
      [[ "$#" -le 0 ]] && echo "no duration provided" && exit 1
      duration=$1
      ;;
    --latency)
      shift
      [[ "$#" -le 0 ]] && echo "no latency provided" && exit 1
      latency=$1
      ;;
    --latency-variation)
      shift
      [[ "$#" -le 0 ]] && echo "no latency variation provided" && exit 1
      latency_variation=$1
      ;;
    --loss)
      shift
      [[ "$#" -le 0 ]] && echo "no loss probability provided" && exit 1
      loss=$1
      ;;
    --duplicate)
      shift
      [[ "$#" -le 0 ]] && echo "no duplicate provided" && exit 1
      duplicate=$1
      ;;
    --cpuprofile)
      shift
      netstack_opts="${netstack_opts} -cpuprofile=$1"
      checktmp "$1"
      ;;
    --memprofile)
      shift
      netstack_opts="${netstack_opts} -memprofile=$1"
      checktmp "$1"
      ;;
    --blockprofile)
      shift
      netstack_opts="${netstack_opts} -blockprofile=$1"
      checktmp "$1"
      ;;
    --mutexprofile)
      shift
      netstack_opts="${netstack_opts} -mutexprofile=$1"
      checktmp "$1"
      ;;
    --traceprofile)
      shift
      netstack_opts="${netstack_opts} -traceprofile=$1"
      checktmp "$1"
      ;;
    --disable-linux-gso)
      disable_linux_gso=1
      ;;
    --disable-linux-gro)
      disable_linux_gro=1
      ;;
    --gro)
      gro=true
      ;;
    --ipv6)
      client_addr=fd::1
      client_proxy_addr=fd::2
      server_proxy_addr=fd::3
      server_addr=fd::4
      full_server_addr=[${server_addr}]:${iperf_port}
      full_server_proxy_addr=[${server_proxy_addr}]:${proxy_port}
      iperf_version_arg=-V
      netstack_opts="${netstack_opts} -ipv6"
      ;;
    --num-client-threads)
      shift
      num_client_threads=$1
      ;;
    --helpers)
      shift
      [[ "$#" -le 0 ]] && echo "no helper dir provided" && exit 1
      helper_dir=$1
      ;;
    --iperf-binary)
      shift
      [[ "$#" -le 0 ]] && echo "no iperf name provided" && exit 1
      iperf_binary_name=$1
      ;;
    --sniff)
      netstack_opts="${netstack_opts} -sniff"
      ;;
    --xdp)
      xdp=true
      ;;
    --no-user-ns)
      unshare_opts=()
      ;;
    *)
      echo "unknown option: $1"
      echo ""
      echo "usage: $0 [options]"
      echo "options:"
      echo " --help                show this message"
      echo " --verbose             verbose output"
      echo " --client              use netstack as the client"
      echo " --second-client       add a linux or netstack sender sharing the same WAN (requires --output-dir)"
      echo " --second-congestion-control  second sender's reno or cubic (default: first sender's choice)"
      echo " --second-start-delay  seconds before starting the second flow (default 0, less than duration)"
      echo " --linux-client        print client stats in linux case"
      echo " --ideal               reset all network emulation"
      echo " --server              use netstack as the server"
      echo " --mtu                 set the mtu (bytes)"
      echo " --sack                enable SACK support"
      echo " --rack                enable RACK support"
      echo " --moderate-recv-buf   enable TCP receive buffer auto-tuning"
      echo " --congestion-control  reno (default) or cubic for both native and Netstack"
      echo " --cubic               alias for --congestion-control cubic"
      echo " --rate                link rate in Mbit/s in each direction (default unlimited)"
      echo " --queue-packets       netem queue limit in each direction (default 1000)"
      echo " --seed                netem random seed (requires kernel and tc support)"
      echo " --output-dir          retain settings, versions, qdisc counters and raw iperf output"
      echo " --latency-probe       retain native ICMP RTT before/during/after the client operation (requires --output-dir)"
      echo " --duration            set the test duration (s)"
      echo " --latency             set the latency (ms)"
      echo " --latency-variation   set the latency variation"
      echo " --loss                set the loss probability (%)"
      echo " --duplicate           set the duplicate probability (%)"
      echo " --helpers             set the helper directory"
      echo " --num-client-threads  number of parallel client threads to run"
      echo " --disable-linux-gso   disable segmentation offload (TSO, GSO, GRO) in the Linux network stack"
      echo " --disable-linux-gro   disable GRO in the Linux network stack"
      echo " --gro                 enable gVisor GRO"
      echo " --ipv6                use ipv6 for benchmarks"
      echo " --iperf-binary        name of the iperf binary to call"
      echo " --sniff               sniff and output packet logs"
      echo " --xdp                 use AF_XDP socket instead of AF_PACKET"
      echo " --no-user-ns          don't run in a new user namespace. Useful for testing as root"
      echo ""
      echo "The output will of the script will be:"
      echo "  BenchmarkTCP/.../stack=.../cc=... 1 <receiver-Mb/s> Mb/s <proxy-CPU-seconds/duration> cpu-time"
      exit 1
  esac
  shift
done

case "$congestion_control" in
  reno|cubic) ;;
  *) echo "unsupported congestion control: $congestion_control" >&2; exit 1 ;;
esac
if [[ -n $rate_mbps && ! $rate_mbps =~ ^[0-9]+([.][0-9]+)?$ ]] ||
   [[ ! $queue_packets =~ ^[1-9][0-9]*$ || ! $duration =~ ^[1-9][0-9]*$ || ! $num_client_threads =~ ^[1-9][0-9]*$ ]] ||
   [[ -n $seed && ! $seed =~ ^[0-9]+$ ]]; then
  echo "rate must be numeric, duration/queue limit/stream count positive integers, and seed unsigned" >&2
  exit 1
fi
case "$second_client" in
  ""|linux|netstack) ;;
  *) echo "--second-client must be linux or netstack" >&2; exit 1 ;;
esac
if [[ ! $second_start_delay =~ ^[0-9]+$ ]]; then
  echo "--second-start-delay must be a nonnegative integer" >&2
  exit 1
fi
second_start_delay=$((10#$second_start_delay))
if [[ -n $second_client ]]; then
  second_congestion_control=${second_congestion_control:-$congestion_control}
  case "$second_congestion_control" in
    reno|cubic) ;;
    *) echo "unsupported second congestion control: $second_congestion_control" >&2; exit 1 ;;
  esac
  if [[ -z $output_dir || -n $iperf_version_arg ]] || $server || $xdp; then
    echo "shared-WAN mode requires --output-dir, IPv4, AF_PACKET and a native server" >&2
    exit 1
  fi
  if (( second_start_delay >= duration )); then
    echo "second flow must start before the first flow's requested duration ends" >&2
    exit 1
  fi
  if [[ $netstack_opts == *profile=* || $netstack_opts == *tcp_probe_file=* ]]; then
    echo "per-proxy profile files are not supported in shared-WAN mode" >&2
    exit 1
  fi
elif [[ -n $second_congestion_control || $second_start_delay != 0 ]]; then
  echo "second-flow options require --second-client" >&2
  exit 1
fi
if $latency_probe && [[ -z $output_dir ]]; then
  echo "--latency-probe requires --output-dir" >&2
  exit 1
fi
if $latency_probe && $xdp; then
  echo "--latency-probe requires AF_PACKET mode, without --xdp" >&2
  exit 1
fi
if [[ -n $output_dir ]]; then
  mkdir -p "$output_dir"
  output_dir=$(cd "$output_dir" && pwd -P)
  checktmp "$output_dir"
fi

if [[ ${verbose} == "true" ]]; then
  set -x
fi

# Latency needs to be halved, since it's applied on both ways.
half_latency=$(echo "${latency}"/2 | bc -l | awk '{printf "%1.2f", $0}')
half_loss=$(echo "${loss}"/2 | bc -l | awk '{printf "%1.6f", $0}')
half_duplicate=$(echo "${duplicate}"/2 | bc -l | awk '{printf "%1.6f", $0}')
helper_dir="${helper_dir#$(pwd)/}" # Use relative paths.
proxy_binary="${helper_dir}/tcp_proxy"
nsjoin_binary="${helper_dir}/nsjoin"

if [[ ! -e ${proxy_binary} ]]; then
  echo "Could not locate ${proxy_binary}, please make sure you've built the binary"
  exit 1
fi

if [[ ! -e ${nsjoin_binary} ]]; then
  echo "Could not locate ${nsjoin_binary}, please make sure you've built the binary"
  exit 1
fi

record_settings() {
  printf 'command: '
  printf '%q ' "${command_line[@]}"
  printf '\n'
  printf 'netstack_options=%s gso=%s swgso=%s gro=%s xdp=%s disable_linux_gso=%s disable_linux_gro=%s\n' \
    "$netstack_opts" "$gso" "$swgso" "$gro" "$xdp" "${disable_linux_gso:-0}" "${disable_linux_gro:-0}"
  printf 'client=%s server=%s congestion_control=%s mtu=%s duration=%s streams=%s\n' \
    "$client" "$server" "$congestion_control" "$mtu" "$duration" "$num_client_threads"
  printf 'rtt_ms=%s jitter_ms=%s loss_percent=%s duplicate_percent=%s rate_mbps=%s queue_packets=%s seed=%s\n' \
    "$latency" "$latency_variation" "$loss" "$duplicate" "${rate_mbps:-unlimited}" "$queue_packets" "${seed:-unspecified}"
  printf 'second_client=%s second_congestion_control=%s second_start_delay=%s\n' \
    "${second_client:-none}" "${second_congestion_control:-none}" "$second_start_delay"
  printf 'latency_probe=%s receiver_json=%s\n' "$latency_probe" "$([[ -n $output_dir ]] && echo true || echo false)"
  if [[ -n $output_dir ]]; then jq --version; fi
  if $latency_probe; then ping -V; fi
  uname -a
  ip -Version
  tc -Version
  "$iperf_binary_name" --version
  sha256sum "$proxy_binary" "$nsjoin_binary"
}
if [[ -n $output_dir ]]; then
  record_settings > "$output_dir/settings.txt" 2>&1
  cat "$output_dir/settings.txt" >&2
else
  record_settings >&2
fi
export TCP_BENCHMARK_OUTPUT_DIR="$output_dir"

if [[ "$(echo "${latency_variation}" | awk '{printf "%1.2f", $0}')" != "0.00" ]]; then
  # As long as there's some jitter, then we use the paretonormal distribution.
  # This will preserve the minimum RTT, but add a realistic amount of jitter to
  # the connection and cause re-ordering, etc. The regular pareto distribution
  # appears to an unreasonable level of delay (we want only small spikes.)
  distribution="distribution paretonormal"
else
  distribution=""
fi

# Client proxy that will listen on the client's iperf target forward traffic
# using the host networking stack.
client_args="${proxy_binary} -congestion_control=${congestion_control} -port ${proxy_port} -forward ${full_server_proxy_addr}"
if ${client}; then
  # Client proxy that will listen on the client's iperf target
  # and forward traffic using netstack.
  client_args="${proxy_binary} -congestion_control=${congestion_control} ${netstack_opts} -port ${proxy_port} -client \\
      -mtu ${mtu} -iface client.0 -addr ${client_proxy_addr} -mask ${mask} \\
      -forward ${full_server_proxy_addr} -gso=${gso} -swgso=${swgso} --gro=${gro} \\
      --xdp=${xdp}"
fi

# Server proxy that will listen on the proxy port and forward to the server's
# iperf server using the host networking stack.
server_args="${proxy_binary} -congestion_control=${congestion_control} -port ${proxy_port} -forward ${full_server_addr}"
if ${server}; then
  # Server proxy that will listen on the proxy port and forward to the servers'
  # iperf server using netstack.
  server_args="${proxy_binary} -congestion_control=${congestion_control} ${netstack_opts} -port ${proxy_port} -server \\
      -mtu ${mtu} -iface server.0 -addr ${server_proxy_addr} -mask ${mask} \\
      -forward ${full_server_addr} -gso=${gso} -swgso=${swgso} --gro=${gro} \\
      --xdp=${xdp}"
fi

# Specify loss and duplicate parameters only if they are non-zero
loss_opt=""
if [[ "$(echo "$half_loss" | bc -q)" != "0" ]]; then
  loss_opt="loss random ${half_loss}%"
fi
duplicate_opt=""
if [[ "$(echo "$half_duplicate" | bc -q)" != "0" ]]; then
  duplicate_opt="duplicate ${half_duplicate}%"
fi
rate_opt=""
if [[ -n $rate_mbps ]]; then rate_opt="rate ${rate_mbps}mbit"; fi
seed_opt=""
if [[ -n $seed ]]; then seed_opt="seed ${seed}"; fi

exec unshare "${unshare_opts[@]}" -m -n -f -p --mount-proc /bin/bash << EOF
set -euo pipefail
set -m

if [[ ${verbose} == "true" ]]; then
  set -x
fi

mount -t tmpfs netstack-bench /tmp

# We may have reset the path in the unshare if the shell loaded some public
# profiles. Ensure that tools are discoverable via the parent's PATH.
export PATH=${PATH}

# Add the server interfaces.
ip link add server.0 type veth peer name server.1

# Add network emulation devices.
ip link add wan.0 type veth peer name wan.1
probe_pid=
flow_pids=()
flow_names=(primary secondary)
bridge_devices=(server.1 wan.0 wan.1)
stop_probe() {
  local status=0 signal_status=0
  if [[ -z \$probe_pid ]]; then return 0; fi
  # The deadline is a backstop. A successful run must retain the probe until
  # its requested drain period ends; an earlier exit is a capture failure.
  kill -INT "\$probe_pid" || signal_status=\$?
  wait "\$probe_pid" || status=\$?
  probe_pid=
  printf '%s\n' "\$status" > "\$TCP_BENCHMARK_OUTPUT_DIR/ping-exit.txt" || return 1
  if (( signal_status != 0 || status > 1 )); then return 1; fi
}
wait_flow() {
  local index=\$1 flow_dir
  # Keep the child status separate from capture failure: a requested TERM is
  # not itself a failure to reap the child during outer cleanup.
  waited_flow_status=0
  wait "\${flow_pids[index]}" || waited_flow_status=\$?
  unset 'flow_pids[index]'
  if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
    flow_dir=\$TCP_BENCHMARK_OUTPUT_DIR
    if [[ -n "${second_client}" ]]; then flow_dir+=/\${flow_names[index]}; fi
    printf '%s\n' "\$waited_flow_status" > "\$flow_dir/flow-exit.txt"
  fi
}
cleanup() {
  local status=\$? cleanup_status=0
  trap - EXIT
  set +e
  if ! stop_probe; then cleanup_status=1; fi
  for pid in "\${flow_pids[@]}"; do
    if kill -0 "\$pid" 2>/dev/null; then
      if ! kill -TERM "\$pid"; then cleanup_status=1; fi
    fi
  done
  for index in "\${!flow_pids[@]}"; do
    if ! wait_flow "\$index"; then cleanup_status=1; fi
  done
  # Detach before deleting the bridge, including on a failed flow.
  # https://github.com/torvalds/linux/commit/1ce5cce89
  for device in "\${bridge_devices[@]}"; do
    if ! ip link set "\$device" nomaster; then cleanup_status=1; fi
  done
  if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
    name=topology-cleanup-exit.txt
    if ! printf '%s\n' "\$cleanup_status" > "\$TCP_BENCHMARK_OUTPUT_DIR/\$name"; then cleanup_status=1; fi
  fi
  if (( status == 0 )); then status=\$cleanup_status; fi
  exit "\$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ip link set wan.0 up
ip link set wan.1 up

# Enroll on the bridge.
ip link add name br0 type bridge
ip link add name br1 type bridge
ip link set server.1 master br1
ip link set wan.0 master br0
ip link set wan.1 master br1
ip link set br0 up
ip link set br1 up

# Set the MTU appropriately.
ip link set server.0 mtu ${mtu}
ip link set wan.0 mtu ${mtu}
ip link set wan.1 mtu ${mtu}

# Add appropriate latency, loss and duplication.
#
# This is added in at the point of bridge connection.
for device in wan.0 wan.1; do
  # NOTE: We don't support a loss correlation as testing has shown that it
  # actually doesn't work. The man page actually has a small comment about this
  # "It is also possible to add a correlation, but this option is now deprecated
  # due to the noticed bad behavior." For more information see netem(8).
  tc qdisc add dev \$device root netem \\
    limit ${queue_packets} \\
    delay ${half_latency}ms ${latency_variation}ms ${distribution} \\
    ${loss_opt} ${duplicate_opt} ${rate_opt} ${seed_opt}
done

# Both clients attach to the same bridge and WAN queues. Keep their namespace,
# native-address and offload setup identical, including the first client's IPv6
# and Netstack/XDP address ownership rules.
configure_client() {
  local name=\$1 native_addr=\$2 proxy_addr=\$3 uses_netstack=\$4
  local iface=\$name.0 peer=\$name.1 netns=/tmp/\$name.netns
  ip link add "\$iface" type veth peer name "\$peer"
  bridge_devices+=("\$peer")
  ip link set "\$peer" master br0
  ip link set "\$iface" mtu ${mtu}
  touch "\$netns"
  unshare -n mount --bind /proc/self/ns/net "\$netns"
  ip link set dev "\$iface" netns "\$netns"
  if ! \$uses_netstack; then
    # The host must not also answer for a Netstack proxy's address.
    ${nsjoin_binary} "\$netns" ip addr add "\$proxy_addr/${mask}" dev "\$iface"
  fi
  ${nsjoin_binary} "\$netns" ip addr add "\$native_addr/${mask}" dev "\$iface"
  if [[ "${disable_linux_gso}" == 1 ]]; then
    ${nsjoin_binary} "\$netns" ethtool -K "\$iface" tso off
    ${nsjoin_binary} "\$netns" ethtool -K "\$iface" gso off
  fi
  if [[ "${disable_linux_gro}" == 1 ]]; then
    ${nsjoin_binary} "\$netns" ethtool -K "\$iface" gro off
  fi
  ${nsjoin_binary} "\$netns" ip link set "\$iface" up
  ${nsjoin_binary} "\$netns" ip link set lo up
  ip link set "\$peer" up
}
configure_client client ${client_addr} ${client_proxy_addr} ${client}
if [[ -n "${second_client}" ]]; then
  second_uses_netstack=false
  if [[ "${second_client}" == netstack ]]; then second_uses_netstack=true; fi
  configure_client client2 10.0.0.5 10.0.0.6 "\$second_uses_netstack"
fi

# Start a server proxy.
touch /tmp/server.netns
unshare -n mount --bind /proc/self/ns/net /tmp/server.netns
# Move the endpoint into the namespace.
while ip link | grep server.0 > /dev/null; do
  ip link set dev server.0 netns /tmp/server.netns
done
if ! ${server}; then
  # Only add the address to NIC if netstack is not in use. Otherwise the host
  # will also process the inbound SYN and send a RST back.
  ${nsjoin_binary} /tmp/server.netns ip addr add ${server_proxy_addr}/${mask} dev server.0
fi

# Add the server address and bring its interface up.
${nsjoin_binary} /tmp/server.netns ip addr add ${server_addr}/${mask} dev server.0
if [[ "${disable_linux_gso}" == "1" ]]; then
  ${nsjoin_binary} /tmp/server.netns ethtool -K server.0 tso off
  ${nsjoin_binary} /tmp/server.netns ethtool -K server.0 gso off
fi
if [[ "${disable_linux_gro}" == "1" ]]; then
  ${nsjoin_binary} /tmp/server.netns ethtool -K server.0 gro off
fi
${nsjoin_binary} /tmp/server.netns ip link set server.0 up
${nsjoin_binary} /tmp/server.netns ip link set lo up
ip link set dev server.1 up


record_features() {
  printf 'client.0\n'
  ${nsjoin_binary} /tmp/client.netns ethtool -k client.0
  printf 'server.0\n'
  ${nsjoin_binary} /tmp/server.netns ethtool -k server.0
  endpoints=(client server)
  if [[ -n "${second_client}" ]]; then
    printf 'client2.0\n'
    ${nsjoin_binary} /tmp/client2.netns ethtool -k client2.0
    endpoints+=(client2)
  fi
  for endpoint in "\${endpoints[@]}"; do
    printf 'native defaults in %s namespace\n' "\$endpoint"
    ${nsjoin_binary} /tmp/\$endpoint.netns sysctl \\
      net.ipv4.tcp_sack net.ipv4.tcp_recovery net.ipv4.tcp_moderate_rcvbuf \\
      net.ipv4.tcp_rmem net.ipv4.tcp_wmem net.ipv4.tcp_congestion_control
  done
}
if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
  record_features > "\$TCP_BENCHMARK_OUTPUT_DIR/interface-features.txt"
else
  record_features >&2
fi

# Collect the baseline before proxies open their forwarding connections.
# Otherwise the added delay can expire the receiver's control-cookie wait.
record_probe_phase() {
  local uptime ignored
  read -r uptime ignored < /proc/uptime
  printf '%s\t%s\t%s\n' "\$1" "\$uptime" "\$(date +%s.%N)" \
    >> "\$TCP_BENCHMARK_OUTPUT_DIR/ping-phases.tsv"
}
if ${latency_probe}; then
  # These are native addresses even when a proxy uses Netstack. This measures
  # the shared ICMP path, not Netstack TCP RTT or pure queue delay.
  timeout --signal=TERM --kill-after=2s 10s \
    ${nsjoin_binary} /tmp/client.netns ping -n -I ${client_addr} -c 1 -W 2 ${server_addr} \
    > "\$TCP_BENCHMARK_OUTPUT_DIR/ping-preflight.txt" 2>&1
  printf 'phase\tboottime_seconds\tunix_seconds\n' > "\$TCP_BENCHMARK_OUTPUT_DIR/ping-phases.tsv"
  record_probe_phase probe-launch
  ${nsjoin_binary} /tmp/client.netns ping -n -I ${client_addr} -D -O -i 0.1 -s 56 -w $((duration + 45)) ${server_addr} \
    > "\$TCP_BENCHMARK_OUTPUT_DIR/ping.txt" \
    2> "\$TCP_BENCHMARK_OUTPUT_DIR/ping-stderr.txt" &
  probe_pid=\$!
  sleep 5
fi

record_qdiscs() {
  for device in wan.0 wan.1; do
    printf '%s\n' "\$device"
    tc -s -d qdisc show dev "\$device"
  done
}

run_flow() {
  set -euo pipefail
  flow=\$1
  client_pid=
  server_pid=
  iperf_pid=
  operation_pid=
  results_file=
  flow_netns=/tmp/client.netns
  flow_client_addr=${client_addr}
  flow_proxy_port=${proxy_port}
  flow_iperf_port=${iperf_port}
  flow_duration=${duration}
  flow_cc=${congestion_control}
  flow_stack=linux
  if ${client}; then flow_stack=netstack; fi
  flow_label=
  client_command=(${client_args})
  server_command=(${server_args})
  if [[ -n "${second_client}" ]]; then
    export TCP_BENCHMARK_OUTPUT_DIR="\$TCP_BENCHMARK_OUTPUT_DIR/\$flow"
    flow_label="flow=\$flow/"
  fi
  if [[ \$flow == secondary ]]; then
    flow_netns=/tmp/client2.netns
    flow_client_addr=10.0.0.5
    flow_proxy_port=$((proxy_port + 1))
    flow_iperf_port=$((iperf_port + 1))
    flow_duration=$((duration - second_start_delay))
    flow_cc=${second_congestion_control}
    flow_stack=${second_client}
    server_command=("${proxy_binary}" "-congestion_control=\$flow_cc" -port "\$flow_proxy_port" -forward "${server_addr}:\$flow_iperf_port")
    client_command=("${proxy_binary}" "-congestion_control=\$flow_cc" -port "\$flow_proxy_port" -forward "${server_proxy_addr}:\$flow_proxy_port")
    if [[ \$flow_stack == netstack ]]; then
      client_command+=(${netstack_opts} -client -mtu ${mtu} -iface client2.0 -addr 10.0.0.6 -mask ${mask} -gso=${gso} -swgso=${swgso} --gro=${gro} --xdp=false)
    fi
  fi
  record_flow_phase() {
    local uptime ignored
    read -r uptime ignored < /proc/uptime
    printf '%s\t%s\t%s\n' "\$1" "\$uptime" "\$(date +%s.%N)" >> "\$TCP_BENCHMARK_OUTPUT_DIR/flow-phases.tsv"
  }
  if [[ -n "${second_client}" ]]; then
    printf 'phase\tboottime_seconds\tunix_seconds\n' > "\$TCP_BENCHMARK_OUTPUT_DIR/flow-phases.tsv"
    printf 'flow=%s client=%s congestion_control=%s duration=%s streams=%s\n' "\$flow" "\$flow_stack" "\$flow_cc" "\$flow_duration" '${num_client_threads}' > "\$TCP_BENCHMARK_OUTPUT_DIR/flow-settings.txt"
    {
      printf 'client_command: '
      printf '%q ' "\${client_command[@]}"
      printf '\nserver_command: '
      printf '%q ' "\${server_command[@]}"
      printf '\n'
    } >> "\$TCP_BENCHMARK_OUTPUT_DIR/flow-settings.txt"
    record_flow_phase proxy-start
  fi
  cleanup_flow() {
    local status=\$? cleanup_status=0
    trap - EXIT
    set +e
    if [[ -n \$operation_pid ]]; then
      if kill -0 "\$operation_pid" 2>/dev/null; then
        if ! kill -TERM "\$operation_pid"; then cleanup_status=1; fi
      fi
      operation_status=0
      wait "\$operation_pid" || operation_status=\$?
      if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
        if ! printf '%s\n' "\$operation_status" > "\$TCP_BENCHMARK_OUTPUT_DIR/iperf-exit.txt"; then cleanup_status=1; fi
      fi
    fi
    for pid in "\$client_pid" "\$server_pid"; do
      if [[ -n \$pid ]]; then
        if ! kill -TERM "\$pid"; then cleanup_status=1; fi
      fi
    done
    for pid in "\$client_pid" "\$server_pid"; do
      if [[ -n \$pid ]]; then
        if ! wait "\$pid"; then cleanup_status=1; fi
      fi
    done
    if [[ -n \$iperf_pid ]]; then
      if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
        # timeout forwards termination to the one-off receiver and bounds
        # shutdown even when its control connection is incomplete.
        if kill -0 "\$iperf_pid" 2>/dev/null; then
          if ! kill -TERM "\$iperf_pid"; then cleanup_status=1; fi
        fi
        receiver_status=0
        wait "\$iperf_pid" || receiver_status=\$?
        if ! printf '%s\n' "\$receiver_status" > "\$TCP_BENCHMARK_OUTPUT_DIR/receiver-exit.txt"; then cleanup_status=1; fi
      else
        if ! kill -9 "\$iperf_pid"; then cleanup_status=1; fi
        receiver_status=0
        wait "\$iperf_pid" || receiver_status=\$?
        if (( receiver_status != 137 )); then cleanup_status=1; fi
      fi
      iperf_pid=
    fi
    if [[ -n \$results_file && -z \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
      if ! rm -f "\$results_file"; then cleanup_status=1; fi
    fi
    if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
      if ! printf '%s\n' "\$cleanup_status" > "\$TCP_BENCHMARK_OUTPUT_DIR/cleanup-exit.txt"; then cleanup_status=1; fi
    fi
    if (( status == 0 )); then status=\$cleanup_status; fi
    exit "\$status"
  }
  trap cleanup_flow EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  ${nsjoin_binary} /tmp/server.netns "\${server_command[@]}" &
  server_pid=\$!

  # A one-off receiver emits one complete JSON report, including receiver-side
  # intervals and per-stream results. Preserve the client's usual text output.
  if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
    timeout --signal=TERM --kill-after=5s \$((flow_duration + 60))s \
      ${nsjoin_binary} /tmp/server.netns ${iperf_binary_name} ${iperf_version_arg} \
      -p \$flow_iperf_port -s -1 -J -i 1 \
      > "\$TCP_BENCHMARK_OUTPUT_DIR/receiver.json" \
      2> "\$TCP_BENCHMARK_OUTPUT_DIR/receiver-stderr.txt" &
  else
    ${nsjoin_binary} /tmp/server.netns ${iperf_binary_name} ${iperf_version_arg} -p \$flow_iperf_port -s >&2 &
  fi
  iperf_pid=\$!

  # Give services time to start.
  sleep 5

  ${nsjoin_binary} "\$flow_netns" "\${client_command[@]}" &
  client_pid=\$!

  # Show traffic information for the original uninstrumented native mode.
  if [[ -z "${second_client}" ]] && ! ${latency_probe} && ! ${client} && ! ${server}; then
    ${nsjoin_binary} /tmp/client.netns ping -c 100 -i 0.001 -W 1 ${server_addr} >&2 || true
  fi

  if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
    results_file="\$TCP_BENCHMARK_OUTPUT_DIR/iperf.txt"
  else
    results_file=\$(mktemp)
  fi

  # Bound the whole client operation, including a blocked connection/control
  # exchange, while leaving the requested measurement interval unchanged.
  run_client() {
    local connect_deadline=\$((SECONDS + 30)) result
    while true; do
      result=0
      "\$@" > "\$results_file" 2>&1 || result=\$?
      if grep -Eq "connect failed|unable to connect" "\$results_file" && (( SECONDS < connect_deadline )); then
        sleep 0.1
        continue
      fi
      return "\$result"
    done
  }
  export -f run_client
  export results_file
  result=0
  if ${latency_probe} && [[ -z "${second_client}" ]]; then record_probe_phase client-operation-begin; fi
  if [[ -n "${second_client}" ]]; then record_flow_phase client-operation-begin; fi
  timeout --signal=TERM --kill-after=5s \$((flow_duration + 30))s \\
    /bin/bash -c 'run_client "\$@"' _ \\
    ${nsjoin_binary} "\$flow_netns" ${iperf_binary_name} \\
    ${iperf_version_arg} -p \$flow_proxy_port -c \$flow_client_addr -t \$flow_duration -f m -P ${num_client_threads} &
  operation_pid=\$!
  wait "\$operation_pid" || result=\$?
  operation_pid=
  if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
    printf '%s\n' "\$result" > "\$TCP_BENCHMARK_OUTPUT_DIR/iperf-exit.txt"
  fi
  if ${latency_probe} && [[ -z "${second_client}" ]]; then record_probe_phase client-operation-end; fi
  if [[ -n "${second_client}" ]]; then record_flow_phase client-operation-end; fi
  cat "\$results_file" >&2
  if (( result != 0 )); then exit "\$result"; fi
  if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
    receiver_status=0
    wait "\$iperf_pid" || receiver_status=\$?
    printf '%s\n' "\$receiver_status" > "\$TCP_BENCHMARK_OUTPUT_DIR/receiver-exit.txt"
    iperf_pid=
    if (( receiver_status != 0 )); then exit "\$receiver_status"; fi
    # Reject an incomplete report, including valid JSON carrying an iperf error.
    # Zero received bytes are a measurement, not a report-format error.
    jq -e --argjson streams "${num_client_threads}" '
      def receiver:
        .sender == false and
        (.bytes | type == "number") and .bytes >= 0 and
        (.seconds | type == "number") and .seconds > 0 and
        (.bits_per_second | type == "number") and .bits_per_second >= 0;
      (has("error") | not) and
      .start.test_start.num_streams == \$streams and
      (.end.streams | length) == \$streams and
      (.end.sum_received | receiver) and
      all(.end.streams[]; .receiver | receiver) and
      (.intervals | type == "array" and length > 0)
    ' "\$TCP_BENCHMARK_OUTPUT_DIR/receiver.json" > /dev/null
  fi

  # Report delivered goodput. The final receiver row is the aggregate for -P.
  mbits=\$(awk '/Mbits\/sec/ && /receiver/ { for (i=2; i<=NF; i++) if (\$i == "Mbits/sec") value=\$(i-1) } END { print value }' "\$results_file")
  if [[ ! \$mbits =~ ^[0-9]+([.][0-9]+)?\$ ]]; then
    echo "No receiver throughput in successful iperf output" >&2
    exit 1
  fi
  client_cpu_ticks=\$(cat /proc/\$client_pid/stat \\
    | awk '{print (\$14+\$15);}')
  server_cpu_ticks=\$(cat /proc/\$server_pid/stat \\
    | awk '{print (\$14+\$15);}')
  ticks_per_sec=\$(getconf CLK_TCK)
  client_cpu_load=\$(bc -l <<< \$client_cpu_ticks/\$ticks_per_sec/\$flow_duration)
  server_cpu_load=\$(bc -l <<< \$server_cpu_ticks/\$ticks_per_sec/\$flow_duration)

  hostgso=true
  if [[ "${disable_linux_gso}" ]]; then
    hostgso=false
  fi
  hostgro=true
  if [[ "${disable_linux_gro}" ]]; then
    hostgro=false
  fi

  if [[ -n "${second_client}" ]] || ${client}; then
    echo "BenchmarkTCP/\${flow_label}role=client/stack=\$flow_stack/cc=\$flow_cc/host-gso=\$hostgso/host-gro=\$hostgro 1 \$mbits Mb/s \$client_cpu_load cpu-time"
    exit 0
  elif ${linux_client}; then
    echo "BenchmarkTCP/\${flow_label}role=client/stack=linux/cc=\$flow_cc/host-gso=\$hostgso/host-gro=\$hostgro 1 \$mbits Mb/s \$client_cpu_load cpu-time"
    exit 0
  fi
  stack=linux
  if ${server}; then stack=netstack; fi
  echo "BenchmarkTCP/\${flow_label}role=server/stack=\$stack/cc=\$flow_cc/host-gso=\$hostgso/host-gro=\$hostgro 1 \$mbits Mb/s \$server_cpu_load cpu-time"
}

if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
  record_qdiscs > "\$TCP_BENCHMARK_OUTPUT_DIR/qdisc-before.txt"
fi
result=0
if [[ -n "${second_client}" ]]; then
  mkdir -p "\$TCP_BENCHMARK_OUTPUT_DIR/primary" "\$TCP_BENCHMARK_OUTPUT_DIR/secondary"
  if ${latency_probe}; then record_probe_phase flows-begin; fi
  run_flow primary > "\$TCP_BENCHMARK_OUTPUT_DIR/primary/benchmark.txt" 2> "\$TCP_BENCHMARK_OUTPUT_DIR/primary/stderr.txt" &
  flow_pids+=(\$!)
  sleep ${second_start_delay}
  run_flow secondary > "\$TCP_BENCHMARK_OUTPUT_DIR/secondary/benchmark.txt" 2> "\$TCP_BENCHMARK_OUTPUT_DIR/secondary/stderr.txt" &
  flow_pids+=(\$!)
else
  run_flow primary &
  flow_pids+=(\$!)
fi
for index in "\${!flow_pids[@]}"; do
  capture_status=0
  wait_flow "\$index" || capture_status=\$?
  if (( waited_flow_status != 0 && result == 0 )); then result=\$waited_flow_status; fi
  if (( capture_status != 0 && result == 0 )); then result=\$capture_status; fi
done
if [[ -n "${second_client}" ]]; then
  if ${latency_probe}; then record_probe_phase flows-end; fi
  for flow in primary secondary; do
    cat "\$TCP_BENCHMARK_OUTPUT_DIR/\$flow/benchmark.txt"
  done
fi
if ${latency_probe}; then
  if (( result == 0 )); then sleep 2; fi
  record_probe_phase probe-stop
  probe_status=0
  stop_probe || probe_status=\$?
  if (( result == 0 )); then result=\$probe_status; fi
fi
if [[ -n \$TCP_BENCHMARK_OUTPUT_DIR ]]; then
  record_qdiscs > "\$TCP_BENCHMARK_OUTPUT_DIR/qdisc-after.txt"
fi
exit "\$result"

EOF
