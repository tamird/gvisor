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
stock=757d67e79ca401d6d662c470d6470d13f9e23a45
fix=7d7f552333cf0aa6fa9563577b97adbda779eff4
git diff --exit-code "$fix" HEAD -- pkg runsc tools .bazelrc MODULE.bazel
printf '%s\n' "$stock" "$fix" > "$out/production.txt"
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
bazel run --jobs=5 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
helper_dir="$RUNNER_TEMP/tcp-benchmark-helpers/fixed"
mkdir -p "$helper_dir"
for tool in tcp_proxy nsjoin; do
  paths=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
  mapfile -t outputs <<< "$paths"
  [[ ${#outputs[@]} == 1 && -x ${outputs[0]} ]]
  cp "${outputs[0]}" "$helper_dir/$tool"
  sha256sum "$helper_dir/$tool" | awk -v tool="$tool" -v source="$fix" '{print source,tool,$1}' >> "$out/binaries.txt"
done
run_pair() {
  local dir=$1 duration=$2 limit=$3 mode=$4 flag=$5 cc=$6 other=$7
  shift 7
  mkdir -p "$dir"
  local ec=0
  sudo -n timeout --signal=INT --kill-after=15s "${limit}s" "$RUNNER_TEMP/start-tcp-trial" --sender-observation "$mode" "$flag" --no-user-ns --congestion-control "$cc" --second-client "$other" --second-congestion-control "$cc" --second-start-delay 0 --latency-probe --output-dir "$dir/results" --ideal --mtu 1500 --latency 100 --rate 20 --queue-packets "$limit_packets" --duration "$duration" --num-client-threads 1 --sack --disable-linux-gso --disable-linux-gro "$@" > "$dir/stdout" 2> "$dir/stderr" || ec=$?
  sudo -n chown -hR "$(id -u):$(id -g)" "$dir"
  echo "$ec" > "$dir/exit.txt"
  return "$ec"
}
phase=preflight
limit_packets=100
for pairing in netstack-netstack linux-linux; do
  primary=${pairing%-*}
  other=${pairing#*-}
  flag=--client; [[ $primary != linux ]] || flag=--linux-client
  run_pair "$out/preflight/$pairing" 5 65 plain "$flag" reno "$other" --helpers "$helper_dir"
done
phase=trials
cases=(reno-netstack-linux cubic-linux-netstack cubic-netstack-linux reno-linux-netstack reno-linux-linux cubic-netstack-netstack cubic-linux-linux reno-netstack-netstack)
status=0
for round in 0 1; do
  for pos in 0 1 2 3 4 5 6 7; do
    idx=$(((pos+3*round)%8))
    name=${cases[idx]}
    IFS=- read -r cc primary other <<< "$name"
    if [[ $round == 1 && $primary == "$other" ]]; then continue; fi
    flag=--client; [[ $primary != linux ]] || flag=--linux-client
    limits=(100 256)
    if (( (idx + round) % 2 )); then limits=(256 100); fi
    for limit_packets in "${limits[@]}"; do
      trial_dir="$out/$round/$limit_packets/plain/$name"
      if ! run_pair "$trial_dir" 120 180 plain "$flag" "$cc" "$other" --helpers "$helper_dir"; then
        tail -10 "$trial_dir/stdout" "$trial_dir/stderr"
        status=1
        [[ -s "$trial_dir/results/qdisc-before.txt" ]] || exit 1
      fi
    done
  done
done
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
