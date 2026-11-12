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
stock=99058130824e4d3166bf0e376f9b15b6ad50146c
fixed=ffac28a03554b62419747c41f389bdc9bfe303d9
[[ "$(git rev-parse HEAD~2)" == "$fixed" && "$(git rev-parse "$fixed^")" == "$stock" ]]
git diff --exit-code "$fixed" HEAD -- . ':(exclude)test/benchmarks/tcp' ':(exclude).github/workflows/build.yml'
public="$RUNNER_TEMP/tcp-public"
mkdir -p "$public"
python3 -B test/benchmarks/tcp/qualification_packages.py "$out/packages" "$public/package-inputs.json" tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
public="$RUNNER_TEMP/tcp-public"
printf '%s\n' "$stock" "$fixed" "$GITHUB_SHA" > "$public/source-heads"
changed=(pkg/tcpip/transport/tcp/{rack,snd,state}.go pkg/tcpip/transport/tcp/test/e2e/{BUILD,tcp_rack_test.go})
for variant in stock fixed; do
  ref=${!variant}
  git restore --source="$ref" -- "${changed[@]}"
  git diff --exit-code "$ref" -- . ':(exclude)test/benchmarks/tcp' ':(exclude).github/workflows/build.yml'
  git apply test/benchmarks/tcp/rto-observer.patch
  git diff --no-ext-diff "$ref" -- pkg/tcpip/transport/tcp/snd.go > "$public/$variant-observer.patch"
  helpers="$RUNNER_TEMP/tcp-benchmark-helpers/$variant"
  mkdir -p "$helpers"
  bazel build --jobs=5 //test/benchmarks/tcp:{tcp_proxy,nsjoin}
  for tool in tcp_proxy nsjoin; do
    path=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
    test -x "$path"
    cp "$path" "$helpers/$tool"
  done
  grep -a -q "WANRTO ns=" "$helpers/tcp_proxy"
  git apply --reverse test/benchmarks/tcp/rto-observer.patch
  cp "$RUNNER_TEMP/tcp-benchmark-helpers/stock/tcp_proxy" "$helpers/tcp_native_proxy"
  sha256sum "$helpers"/{tcp_proxy,tcp_native_proxy,nsjoin} >> "$public/helpers.sha256"
  printf '%s %s %s\n' "$variant" "$ref" "$(git rev-parse "$ref:pkg/tcpip/transport/tcp")" >> "$public/sender-trees"
done
base_helpers="$RUNNER_TEMP/tcp-benchmark-helpers"
cmp "$base_helpers/stock/tcp_native_proxy" "$base_helpers/fixed/tcp_native_proxy"
cmp "$base_helpers/stock/nsjoin" "$base_helpers/fixed/nsjoin"
if cmp -s "$base_helpers/stock/tcp_proxy" "$base_helpers/fixed/tcp_proxy"; then exit 1; fi
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
  variants=(stock fixed)
  if (( pair % 2 == 0 )); then variants=(fixed stock); fi
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
