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
fixed=ffac28a03554b62419747c41f389bdc9bfe303d9
[[ "$(git rev-parse HEAD~3)" == "$fixed" ]]
git diff --exit-code "$fixed" HEAD -- . ':(exclude)test/benchmarks/tcp' ':(exclude).github/workflows/build.yml'
public="$RUNNER_TEMP/tcp-public"
mkdir -p "$public"
python3 -B test/benchmarks/tcp/qualification_packages.py "$out/packages" "$public/package-inputs.json" tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
public="$RUNNER_TEMP/tcp-public"
printf '%s\n' "$fixed" "$fixed" "$GITHUB_SHA" > "$public/source-heads"
for variant in control observed; do
  git apply test/benchmarks/tcp/rto-observer.patch
  if [[ "$variant" == observed ]]; then git apply test/benchmarks/tcp/ack-allowance-observer.patch; fi
  git diff --no-ext-diff "$fixed" -- pkg/tcpip/transport/tcp/snd.go > "$public/$variant-observer.patch"
  helpers="$RUNNER_TEMP/tcp-benchmark-helpers/$variant"
  mkdir -p "$helpers"
  bazel build --jobs=5 //test/benchmarks/tcp:{tcp_proxy,nsjoin}
  for tool in tcp_proxy nsjoin; do
    path=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
    test -x "$path"
    cp "$path" "$helpers/$tool"
  done
  grep -a -q "WANRTO ns=" "$helpers/tcp_proxy"
  if [[ "$variant" == observed ]]; then
    grep -a -q "WANACK ns=" "$helpers/tcp_proxy"
    git apply --reverse test/benchmarks/tcp/ack-allowance-observer.patch
  fi
  git apply --reverse test/benchmarks/tcp/rto-observer.patch
  cp "$RUNNER_TEMP/tcp-benchmark-helpers/control/tcp_proxy" "$helpers/tcp_native_proxy"
  sha256sum "$helpers"/{tcp_proxy,tcp_native_proxy,nsjoin} >> "$public/helpers.sha256"
  printf '%s %s %s\n' "$variant" "$fixed" "$(git rev-parse "$fixed:pkg/tcpip/transport/tcp")" >> "$public/sender-trees"
done
base_helpers="$RUNNER_TEMP/tcp-benchmark-helpers"
cmp "$base_helpers/control/tcp_native_proxy" "$base_helpers/observed/tcp_native_proxy"
cmp "$base_helpers/control/nsjoin" "$base_helpers/observed/nsjoin"
if cmp -s "$base_helpers/control/tcp_proxy" "$base_helpers/observed/tcp_proxy"; then exit 1; fi
git diff --exit-code HEAD -- .
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
for pair in {1..8}; do
  variants=(control observed)
  if (( pair % 2 == 0 )); then variants=(observed control); fi
  for variant in "${variants[@]}"; do
    printf 'primary%d-%s cubic primary\n' "$pair" "$variant"
  done
done > "$public/schedule.txt"
layout=combined
while read -r arm cc first; do
  variant=${arm#*-}
  helpers="$base_helpers/$variant"
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
