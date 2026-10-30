#!/bin/bash
set -euo pipefail
out="$RUNNER_TEMP/qualification/current-shared"
mkdir -p "$out"
phase=setup
finish() {
  status=$?
  trap - EXIT
  printf '{"phase":"%s","status":%d,"finished":"%s"}\n' "$phase" "$status" "$(date -u +%FT%TZ)" > "$out/completion.json"
  exit "$status"
}
trap finish EXIT
[[ "$(git rev-parse HEAD)" == "$GITHUB_SHA" ]]
base=d8ff478596acc888f2b90baa7769d78debe1f512
stock=c4de0f977a6c865d4eb4da2031e54927a3862fba
zero=3a32c606a89b3db602715f45bd3181d29055f401
[[ "$(git diff --name-only "$stock" "$zero")" == pkg/tcpip/transport/tcp/rack.go ]]
git diff --exit-code "$base" "$stock" -- pkg runsc tools .bazelrc MODULE.bazel
git diff --exit-code "$zero" HEAD -- pkg
printf '%s\n' "$base" > "$out/production.txt"
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
printf '%s\n' "$base" "$stock" "$zero" "$GITHUB_SHA" > "$RUNNER_TEMP/tcp-public/source-heads"
for variant in stock zero; do
  sourcesha=$stock
  [[ "$variant" == stock ]] || sourcesha=$zero
  git show "$sourcesha:pkg/tcpip/transport/tcp/rack.go" > pkg/tcpip/transport/tcp/rack.go
  git diff --exit-code "$sourcesha" -- pkg
  bazel run --jobs=5 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
helpers="$RUNNER_TEMP/tcp-benchmark-helpers/$variant"
mkdir -p "$helpers" "$RUNNER_TEMP/tcp-public"
for tool in tcp_proxy nsjoin; do
  paths=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
  mapfile -t outputs <<< "$paths"
  (("${#outputs[@]}" == 1))
  test -x "${outputs[0]}"
  cp "${outputs[0]}" "$helpers/$tool"
  sha256sum "$helpers/$tool" >> "$RUNNER_TEMP/tcp-public/helpers.sha256"
done
done
run_pair() {
  local dir=$1 duration=$2 limit=$3 mode=$4 flag=$5 cc=$6 other=$7
  shift 7
  mkdir -p "$dir"
  local ec=0
  sudo -n timeout --signal=INT --kill-after=15s "${limit}s" "$RUNNER_TEMP/start-tcp-trial" --sender-observation "$mode" "$flag" --no-user-ns --congestion-control "$cc" --second-client "$other" --second-congestion-control "$cc" --second-start-delay 0 --latency-probe --output-dir "$dir/results" --ideal --mtu 1500 --latency 100 --rate 20 --queue-packets 100 --duration "$duration" --num-client-threads 1 --sack --disable-linux-gso --disable-linux-gro "$@" > "$dir/stdout" 2> "$dir/stderr" || ec=$?
  sudo -n chown -hR "$(id -u):$(id -g)" "$dir"
  echo "$ec" > "$dir/exit.txt"
  return "$ec"
}
phase=trials
status=0
for arm in a-stock a-zero b-zero b-stock; do
  variant=${arm#*-}
  dir="$out/$arm/measured/reno-netstack"
  mkdir -p "$dir"
  date -u +%FT%TZ > "$dir/start"
  if ! run_pair "$dir" 45 110 measured --client reno linux --helpers "$RUNNER_TEMP/tcp-benchmark-helpers/$variant"; then
    tail -10 "$dir/stdout" "$dir/stderr"
    status=1
  fi
  date -u +%FT%TZ > "$dir/finish"
done
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
