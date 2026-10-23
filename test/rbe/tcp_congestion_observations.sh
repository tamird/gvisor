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
[[ ${QUALIFICATION_COMPARISON_BLOCK:?} == 0 ]]
candidate=${QUALIFICATION_COMMIT:?}
baseline=$(git rev-parse "$candidate^")
out="$RUNNER_TEMP/qualification/tcp-changing-rtt-pilot"
mkdir -p "$out"
export out
finish() {
  local result=$?
  git diff --exit-code > "$out/source-after.diff" || result=1
  git checkout --detach "$candidate" >> "$out/source-restore.txt" 2>&1 || result=1
  [[ $(git rev-parse HEAD) == "$candidate" ]] || result=1
  if [[ -f $out/tool-hashes-before.txt ]]; then
    sha256sum --check --strict "$out/tool-hashes-before.txt" > "$out/tool-hashes-after.txt" 2>&1 || result=1
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
git cat-file -p "$baseline" > "$out/baseline-commit.txt"
git diff "$baseline" "$candidate" > "$out/treatment.patch"
# Only the six approved RTT files may differ; all harness/tool inputs match.
git diff --name-only "$baseline" "$candidate" > "$out/treatment-paths.txt"
printf '%s\n' pkg/tcpip/transport/tcp/connect.go pkg/tcpip/transport/tcp/rack.go pkg/tcpip/transport/tcp/rack_test.go pkg/tcpip/transport/tcp/segment.go pkg/tcpip/transport/tcp/snd.go pkg/tcpip/transport/tcp/test/e2e/tcp_acktiming_test.go > "$out/expected-treatment-paths.txt"
cmp "$out/expected-treatment-paths.txt" "$out/treatment-paths.txt"
for file in test/benchmarks/tcp/tcp_benchmark.sh test/benchmarks/tcp/tcp_proxy.go test/benchmarks/tcp/README.md test/benchmarks/tcp/BUILD tools/bazeldefs/go.bzl tools/bazeldefs/platforms.bzl .bazelrc pkg/tcpip/transport/tcp/cubic.go pkg/tcpip/transport/tcp/reno.go pkg/tcpip/transport/tcp/snd.go pkg/tcpip/transport/tcp/state.go pkg/tcpip/transport/tcp/connect.go test/rbe/actions.sh .github/workflows/build.yml test/rbe/tcp_congestion_observations.sh test/rbe/tcp_changing_rtt_observations.py test/rbe/tcp_changing_rtt_tools.tsv; do
  cp "$file" "$out/source-${file//\//_}"
done
# Pin direct tools to the same package versions as the existing controls.
# The hosted kernel and base dependencies remain observed worker inputs.
mkdir -p "$out/packages"
packages=()
while IFS=$'\t' read -r package version url digest bytes filename; do
  destination="$out/packages/$filename"
  curl --fail --silent --show-error --location --retry 2 --max-time 120 "$url" --output "$destination"
  [[ $(stat -c %s "$destination") == "$bytes" ]]
  printf '%s  %s\n' "$digest" "$destination" | sha256sum --check --strict
  packages+=("$destination")
done < test/rbe/tcp_changing_rtt_tools.tsv
sudo -n apt-get update > "$out/apt-update.txt" 2>&1
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --allow-downgrades \
  "${packages[@]}" > "$out/apt-install.txt" 2>&1
while IFS=$'\t' read -r package version url digest bytes filename; do
  [[ $(dpkg-query -W -f='${Version}' "$package") == "$version" ]]
done < test/rbe/tcp_changing_rtt_tools.tsv
dpkg-query -W > "$out/host-packages.txt"
# Bind executables and their actually resolved shared libraries across arms.
{
  for tool in ip tc iperf3 ethtool bc modprobe jq ping bash unshare mount umount timeout awk grep sed date sleep python3; do
    path=$(command -v "$tool")
    readlink -f "$path"
    ldd "$path" 2>/dev/null | awk '{ for (i=1;i<=NF;i++) if ($i ~ /^\//) print $i }' || true
  done
} | sort -u | xargs sha256sum > "$out/tool-hashes-before.txt"
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
# Both controls and changing trials configure no random loss, jitter or seed.
options=(--config=rbe --config=x86_64 --remote_download_outputs=toplevel)
targets=(//test/benchmarks/tcp:tcp_benchmark //test/benchmarks/tcp:tcp_proxy //test/benchmarks/tcp:nsjoin)
for arm in before after; do
  source=$baseline
  if [[ $arm == after ]]; then source=$candidate; fi
  git checkout --detach "$source"
  [[ $(git rev-parse HEAD) == "$source" ]]
  mkdir -p "$out/build-$arm"
  bazel build "${options[@]}" "--build_metadata=COMMIT_SHA=$source" \
    "--build_event_json_file=$out/build-$arm/bep.jsonl" \
    --build_event_json_file_path_conversion=false "${targets[@]}" \
    > "$out/build-$arm/stdout.txt" 2> "$out/build-$arm/stderr.txt"
  printf '0\n' > "$out/build-$arm/exit.txt"
  bazel cquery "${options[@]}" "--build_metadata=COMMIT_SHA=$source" \
    'set(//test/benchmarks/tcp:tcp_benchmark //test/benchmarks/tcp:tcp_proxy //test/benchmarks/tcp:nsjoin)' \
    --output=files > "$out/build-$arm/outputs.txt" 2> "$out/build-$arm/query-stderr.txt"
  while IFS= read -r file; do sha256sum "$file"; done < "$out/build-$arm/outputs.txt" > "$out/build-$arm/output-hashes.txt"
  git diff --exit-code
  printf '%s\n' "$source" > "$out/build-$arm/source.txt"
done
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
    .end.sum_received.bytes == ([.intervals[].sum.bytes] | add) and
    (.intervals | length > 0) and all(.intervals[]; (.streams | length) == $streams)
  ' "$dir/receiver.json"
  grep -Eq 'Mbits/sec.*receiver' "$dir/iperf.txt"
}
validate_probe() {
  local dir=$1 begin=$2 end=$3
  grep -Eq '^[01]$' "$dir/ping-exit.txt"
  awk -F '\t' -v begin="$begin" -v end="$end" '
    NR == FNR {
      if (FNR == 1) { if ($0 != "phase\tboottime_seconds\tunix_seconds\tboottime_after_seconds") exit 1; next }
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
validate_shared() (
  set -euo pipefail
  local trial=$1 delay=$2 first_stack=$3 first_cc=$4 second_stack=$5 second_cc=$6 duration=$7 arm=$8
  local dir=$trial/results expected_stack expected_cc
  # The actual launched helpers must be the outputs built for this source arm.
  awk '
    NR == FNR {
      name=$2; sub(/^.*\//, "", name)
      if (name == "tcp_proxy" || name == "nsjoin") { expected[name]=$1; count++ }
      next
    }
    $1 ~ /^[0-9a-f]+$/ {
      name=$2; sub(/^.*\//, "", name)
      if (name in expected) { if ($1 != expected[name] || seen[name]++) exit 1; found++ }
    }
    END { if (count != 2 || found != 2) exit 1 }
  ' "$out/build-$arm/output-hashes.txt" "$dir/settings.txt"
  grep -Fxq 0 "$dir/topology-cleanup-exit.txt"
  for flow in primary secondary; do
    validate_receiver "$dir/$flow" 1
    expected_stack=$first_stack
    expected_cc=$first_cc
    if [[ $flow == secondary ]]; then expected_stack=$second_stack; expected_cc=$second_cc; fi
    [[ $(head -n 1 "$dir/$flow/flow-settings.txt") == "flow=$flow client=$expected_stack congestion_control=$expected_cc duration=$duration streams=1" ]]
    grep -Eq "^BenchmarkTCP/flow=$flow/role=client/stack=$expected_stack/cc=$expected_cc/.+ 1 [0-9.]+ Mb/s [0-9.]+ cpu-time$" "$dir/$flow/benchmark.txt"
    if [[ $expected_stack == netstack ]]; then
      grep -Fq "netstack client settings: congestion_control=$expected_cc sack=true rack=true " "$dir/$flow/stderr.txt"
    else
      grep -Fq "client=false, server=false, congestion_control=$expected_cc, ready." "$dir/$flow/stderr.txt"
    fi
    awk -F '\t' '
      NR == 1 { if ($0 != "phase\tboottime_seconds\tunix_seconds\tboottime_after_seconds") exit 1; next }
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
      b1=times[1,"client-operation-begin"]-0.02; e1=times[1,"client-operation-end"]+0.02
      b2=times[2,"client-operation-begin"]-0.02; e2=times[2,"client-operation-end"]+0.02
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
# Four matched controls on the baseline, then one fixed-order treatment pair.
# This is feasibility evidence, not the planned 16-pair performance cohort.
scenarios=(
  'reno-netstack-first before netstack reno linux reno 120 100 false'
  'reno-linux-first before linux reno netstack reno 120 100 false'
  'cubic-netstack-first before netstack cubic linux cubic 120 100 false'
  'cubic-linux-first before linux cubic netstack cubic 120 100 false'
  'changing-before before netstack cubic linux cubic 480 10 true'
  'changing-after after netstack cubic linux cubic 480 10 true'
)
printf 'position\tcase\tarm\tsource\tprimary_stack\tprimary_cc\tsecondary_stack\tsecondary_cc\tduration\tchanging\n' > "$out/trials.tsv"
trial_status=0
position=0
for scenario in "${scenarios[@]}"; do
  read -r name arm first_stack first_cc second_stack second_cc duration latency changing <<< "$scenario"
  source=$baseline
  if [[ $arm == after ]]; then source=$candidate; fi
  git checkout --detach "$source"
  [[ $(git rev-parse HEAD) == "$source" ]]
  position=$((position + 1))
  trial="$out/$name"
  mkdir -p "$trial"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$position" "$name" "$arm" "$source" "$first_stack" "$first_cc" "$second_stack" "$second_cc" "$duration" "$changing" >> "$out/trials.tsv"
  flags=(--linux-client)
  if [[ $first_stack == netstack ]]; then flags=(--client); fi
  profile=()
  limit=180
  if [[ $changing == true ]]; then profile=(--changing-rtt-profile); limit=600; fi
  result=0
  timeout --signal=INT --kill-after=15s "$limit" bash -c 'bazel "$@"' _ run "${options[@]}" \
    "--build_metadata=COMMIT_SHA=$source" \
    "--build_event_json_file=$trial/bep.jsonl" --build_event_json_file_path_conversion=false \
    "--execution_log_compact_file=$trial/run-execution.binpb" \
    //test/benchmarks/tcp:tcp_benchmark -- \
    "${flags[@]}" --no-user-ns --congestion-control "$first_cc" \
    --second-client "$second_stack" --second-congestion-control "$second_cc" \
    --second-start-delay 0 --latency-probe --output-dir "$trial/results" \
    --ideal --mtu 1500 --latency "$latency" --rate 20 --queue-packets 100 \
    --duration "$duration" --num-client-threads 1 --sack \
    --disable-linux-gso --disable-linux-gro "${profile[@]}" \
    > "$trial/stdout.txt" 2> "$trial/stderr.txt" || result=$?
  printf '%s\n' "$result" > "$trial/exit.txt"
  if (( result != 0 )); then trial_status=1; continue; fi
  set +e
  validate_shared "$trial" 0 "$first_stack" "$first_cc" "$second_stack" "$second_cc" "$duration" "$arm" > "$trial/observation-check.txt" 2>&1
  result=$?
  set -e
  printf '%s\n' "$result" > "$trial/observation-check-exit.txt"
  if (( result != 0 )); then trial_status=1; continue; fi
  result=0
  python3 test/rbe/tcp_changing_rtt_observations.py "$trial/results" "$changing" \
    > "$trial/window-observations.json" 2> "$trial/window-observations-stderr.txt" || result=$?
  printf '%s\n' "$result" > "$trial/window-observations-exit.txt"
  if (( result != 0 )); then trial_status=1; fi
done
git diff --exit-code > "$out/source-after.diff" || trial_status=1
printf '%s\n' "$trial_status" > "$out/final-exit.txt"
exit "$trial_status"
