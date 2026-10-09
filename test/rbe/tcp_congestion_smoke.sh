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
out="$RUNNER_TEMP/qualification/tcp-cubic-rfc9438-comparison"
mkdir -p "$out"
export out
finish() {
  local result=$?
  for name in cubic reno snd state; do
    if [[ -f $out/original-$name.go ]]; then
      cp "$out/original-$name.go" "pkg/tcpip/transport/tcp/$name.go" || result=1
    fi
  done
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
for file in test/benchmarks/tcp/tcp_benchmark.sh test/benchmarks/tcp/tcp_proxy.go test/benchmarks/tcp/README.md test/benchmarks/tcp/BUILD tools/bazeldefs/go.bzl tools/bazeldefs/platforms.bzl .bazelrc; do
  cp "$file" "$out/source-${file//\//_}"
done
for name in cubic reno snd state; do
  cp "pkg/tcpip/transport/tcp/$name.go" "$out/original-$name.go"
done
printf '%s  %s\n' \
  2d58e5a7277b6ba4b2946d5bf3c7a601fc1c9f53baa9885231f4cb996b8d5793 "$out/original-cubic.go" \
  687e06d1fa229deccfbf623820d44eaeabd95c1181c127bf814cffb0b5aa7b82 "$out/original-reno.go" \
  88620ed933425efa34baf809b6920567bf1e05d99159845427894f0eb98d33c9 "$out/original-snd.go" \
  31b1594eb5788a1902b353dffd3b19299951a3c40be3bf57d8931745c0c2b3aa "$out/original-state.go" \
  | sha256sum --check --strict
sudo -n apt-get update > "$out/apt-update.txt" 2>&1
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  iproute2 iperf3 ethtool bc kmod > "$out/apt-install.txt" 2>&1
{
  uname -a
  dpkg-query -W iproute2 iperf3 ethtool bc kmod
  ip -Version
  tc -Version
  iperf3 --version
  ethtool --version
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
# The approved random-loss comparison records seed unavailability; it does
# not claim identical packet histories. Preserve the independent probe above.
options=(--config=rbe --config=x86_64 --remote_download_outputs=toplevel)
fixed_source=20db25c174297921386662c5f2b4a76a8da62ea5
git fetch --no-tags --depth=1 origin "$fixed_source" \
  > "$out/source-fetch.txt" 2>&1
git cat-file -p "$fixed_source" > "$out/fixed-source-commit.txt"
for name in cubic reno snd state; do
  git show "$fixed_source:pkg/tcpip/transport/tcp/$name.go" > "$out/fixed-$name.go"
done
printf '%s  %s\n' \
  8a44c009129b63059c4be55ecd034754b5386c46d8f53eaa0917429f3a432c9f "$out/fixed-cubic.go" \
  aefeeae15594699a57344b342fa62418de6a4cd15752c30838b450caeb310086 "$out/fixed-reno.go" \
  1aad62c4c75d24e0f9261b3cc1aa22fc9fc5f4cb7f509e6979c6b7d45864e3ed "$out/fixed-snd.go" \
  499f2a0181b74748e0afa39b52f5aa24137a29cd2311db95e4cf793d5aacce7f "$out/fixed-state.go" \
  | sha256sum --check --strict
restore_sources() {
  for name in cubic reno snd state; do
    cp "$out/original-$name.go" "pkg/tcpip/transport/tcp/$name.go"
  done
}
for variant in original fixed; do
  restore_sources
  if [[ $variant != original ]]; then
    # The repaired controllers share sender callbacks. Copy the complete
    # production change, rather than combining incompatible interfaces.
    for name in cubic reno snd state; do
      cp "$out/fixed-$name.go" "pkg/tcpip/transport/tcp/$name.go"
    done
  fi
  directory="$out/prebuild-$variant"
  mkdir -p "$directory/helpers"
  cp pkg/tcpip/transport/tcp/{cubic,reno,snd,state}.go "$directory/"
  git diff -- pkg/tcpip/transport/tcp/{cubic,reno,snd,state}.go > "$directory/production.patch"
  bazel build "${options[@]}" \
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
restore_sources
git diff --exit-code > "$out/source-restored.diff"
# Reprime the original configuration before measurement. Every later `run`
# retains a compact trace so a fresh measured compiler is a result failure.
bazel build "${options[@]}" //test/benchmarks/tcp:tcp_benchmark \
  > "$out/reprime-stdout.txt" 2> "$out/reprime-stderr.txt"
trial_status=0
block=0
for order in ABCDE EDCBA CEADB BDAEC DAEBC CBEAD; do
  block=$((block + 1))
  scenarios=(high-bdp-low-loss)
  # One five-mode control checks that both binaries can saturate a small BDP.
  # It is a functional control, not a repeated performance comparison.
  if (( block == 1 )); then scenarios=(small-bdp-control high-bdp-low-loss); fi
  for scenario in "${scenarios[@]}"; do
    case "$scenario" in
      small-bdp-control) rate=20; latency=10; loss=0; queue=100; duration=15 ;;
      high-bdp-low-loss) rate=100; latency=100; loss=0.01; queue=1000; duration=120 ;;
    esac
    for ((position=0; position<${#order}; position++)); do
      label=${order:position:1}
      case "$label" in
        A) variant=original; stack=netstack; cc=cubic ;;
        B) variant=fixed; stack=netstack; cc=cubic ;;
        C) variant=fixed; stack=netstack; cc=reno ;;
        D) variant=original; stack=linux; cc=reno ;;
        E) variant=original; stack=linux; cc=cubic ;;
      esac
      printf -v name 'b%02d-%s-%s' "$block" "$scenario" "$label"
      trial="$out/$name"
      mkdir -p "$trial"
      printf 'block=%s order=%s position=%s scenario=%s label=%s variant=%s stack=%s cc=%s\n' \
        "$block" "$order" "$position" "$scenario" "$label" "$variant" "$stack" "$cc" \
        > "$trial/identity.txt"
      flags=(--linux-client)
      if [[ $stack == netstack ]]; then flags=(--client); fi
      result=0
      timeout --signal=INT --kill-after=15s "$((duration + 60))s" bash -c 'bazel "$@"' _ run "${options[@]}" \
        "--execution_log_compact_file=$trial/run-execution.binpb" \
        //test/benchmarks/tcp:tcp_benchmark -- \
        "${flags[@]}" --no-user-ns --congestion-control "$cc" --ideal \
        --latency "$latency" --loss "$loss" --rate "$rate" --queue-packets "$queue" \
        --duration "$duration" --sack --disable-linux-gso --disable-linux-gro \
        --helpers "$out/prebuild-$variant/helpers" --output-dir "$trial/results" \
        > "$trial/stdout.txt" 2> "$trial/stderr.txt" || result=$?
      printf '%s\n' "$result" > "$trial/exit.txt"
      if (( result != 0 )); then trial_status=1; fi
    done
  done
done
restore_sources
git diff --exit-code > "$out/source-after.diff" || trial_status=1
printf '%s\n' "$trial_status" > "$out/final-exit.txt"
exit "$trial_status"
