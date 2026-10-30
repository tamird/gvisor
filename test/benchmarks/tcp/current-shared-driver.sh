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
mapfile -t changed < <(git diff --name-only "$stock" "$fix")
[[ ${#changed[@]} == 7 ]]
for variant in stock fixed; do
  source=$stock
  if [[ $variant == fixed ]]; then source=$fix; fi
  git restore --source="$source" --worktree -- "${changed[@]}"
  git diff --exit-code "$source" -- pkg runsc tools .bazelrc MODULE.bazel
  bazel run --jobs=5 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
  helper_dir="$RUNNER_TEMP/tcp-benchmark-helpers/$variant"
  mkdir -p "$helper_dir"
  for tool in tcp_proxy nsjoin; do
    paths=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
    mapfile -t outputs <<< "$paths"
    [[ ${#outputs[@]} == 1 && -x ${outputs[0]} ]]
    cp "${outputs[0]}" "$helper_dir/$tool"
    sha256sum "$helper_dir/$tool" | awk -v v="$variant" -v tool="$tool" -v source="$source" '{print v,source,tool,$1}' >> "$out/binaries.txt"
  done
done
! cmp -s "$RUNNER_TEMP/tcp-benchmark-helpers/stock/tcp_proxy" "$RUNNER_TEMP/tcp-benchmark-helpers/fixed/tcp_proxy" || exit 1
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
phase=preflight
for variant in stock fixed; do
  pre="$out/preflight/$variant"
  if ! run_pair "$pre" 5 65 plain --client reno linux --helpers "$RUNNER_TEMP/tcp-benchmark-helpers/$variant"; then
    tail -10 "$pre/stdout" "$pre/stderr"
    exit 1
  fi
done
phase=trials
cases=(reno-netstack cubic-linux cubic-netstack reno-linux)
status=0
for round in 0 1 2; do
  mode=plain
  if [[ $round == 2 ]]; then mode=measured; fi
  for pos in 0 1 2 3; do
    idx=$(((pos+round)%4))
    name=${cases[idx]}
    cc=${name%-*}; primary=${name#*-}
    variants=(stock fixed)
    if (( (idx + round) % 2 )); then variants=(fixed stock); fi
    flag=--client; other=linux
    if [[ $primary == linux ]]; then flag=--linux-client; other=netstack; fi
    for variant in "${variants[@]}"; do
      trial_dir="$out/$round/$variant/$mode/$name"
      if ! run_pair "$trial_dir" 120 180 "$mode" "$flag" "$cc" "$other" --helpers "$RUNNER_TEMP/tcp-benchmark-helpers/$variant"; then
        tail -10 "$trial_dir/stdout" "$trial_dir/stderr"
        status=1
        [[ -s "$trial_dir/results/qdisc-before.txt" ]] || exit 1
      fi
    done
  done
done
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
