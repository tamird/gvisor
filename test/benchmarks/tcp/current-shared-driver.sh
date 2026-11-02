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
source=aa767e4c43d011121271551fe24da501a56f729d
stock=86f288a421f0b672e161c8af570fb6bfce858be9
[[ "$(git rev-parse HEAD^)" == "$stock" ]]
git diff --quiet "$source" "$stock" -- . ':(exclude)test/benchmarks/tcp' ':(exclude).github/workflows/build.yml'
git diff --quiet "$stock" HEAD -- . ':(exclude)test/benchmarks/tcp/tcp_benchmark.sh' ':(exclude)test/benchmarks/tcp/current-shared-driver.sh' ':(exclude)test/benchmarks/tcp/current-shared-report.py' ':(exclude)test/benchmarks/tcp/flight_capture.py' ':(exclude).github/workflows/build.yml'
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
public="$RUNNER_TEMP/tcp-public"
printf '%s\n' "$source" "$stock" "$GITHUB_SHA" > "$public/source-heads"
helpers="$RUNNER_TEMP/tcp-benchmark-helpers/stock"
mkdir -p "$helpers"
bazel build --jobs=5 //test/benchmarks/tcp:{tcp_proxy,nsjoin}
for tool in tcp_proxy nsjoin; do
  paths=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
  mapfile -t outputs <<< "$paths"
  (( "${#outputs[@]}" == 1 ))
  cp "${outputs[0]}" "$helpers/$tool"
  test -x "$helpers/$tool"
done
cp "$helpers/tcp_proxy" "$helpers/tcp_native_proxy"
sha256sum "$helpers"/{tcp_proxy,tcp_native_proxy,nsjoin} > "$public/helpers.sha256"
bazel run --jobs=5 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
cp "$out/worker.txt" "$RUNNER_TEMP/tcp-public/worker.txt"
run_pair() {
  local dir=$1 duration=$2 limit=$3 mode=$4 flag=$5 cc=$6 other=$7
  shift 7
  mkdir -p "$dir"
  local ec=0
  sudo -n env TCP_FIRST_FLOW="$first" TCP_QUEUE_LAYOUT="$layout" timeout --signal=INT --kill-after=15s "${limit}s" "$RUNNER_TEMP/start-tcp-trial" --sender-observation "$mode" "$flag" --no-user-ns --congestion-control "$cc" --second-client "$other" --second-congestion-control "$cc" --second-start-delay 0 --latency-probe --output-dir "$dir/results" --ideal --mtu 1500 --latency 100 --rate 20 --queue-packets 100 --duration "$duration" --num-client-threads 1 --sack --disable-linux-gso --disable-linux-gro "$@" > "$dir/stdout" 2> "$dir/stderr" || ec=$?
  sudo -n chown -hR "$(id -u):$(id -g)" "$dir"
  echo "$ec" > "$dir/exit.txt"
  return "$ec"
}
phase=trials
status=0
printf '%s\n' 'primary1-combined cubic primary' 'primary1-split cubic primary' 'primary2-split cubic primary' 'primary2-combined cubic primary' > "$public/schedule.txt"
while read -r arm cc first; do
  layout=${arm#*-}
  dir="$out/$arm/plain/$cc-netstack"
  mkdir -p "$dir"
  date -u +%FT%TZ > "$dir/start"

  if ! run_pair "$dir" 45 110 plain --client "$cc" linux --helpers "$helpers" --rack; then
    tail -10 "$dir/stdout" "$dir/stderr"
    status=1
  fi
  date -u +%FT%TZ > "$dir/finish"
done < "$RUNNER_TEMP/tcp-public/schedule.txt"
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
