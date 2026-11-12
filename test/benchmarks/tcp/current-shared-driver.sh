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
public=$RUNNER_TEMP/tcp-public
test "$(cat "$public/controls-passed")" = complete
python3 -B test/benchmarks/tcp/qualification_packages.py "$out/packages" "$public/package-inputs.json" tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$public/worker.txt"
base_helpers=$RUNNER_TEMP/tcp-benchmark-helpers
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
for triple in 1 2 3; do
  case "$triple" in
    1) variants=(reference accounting corrected);;
    2) variants=(accounting corrected reference);;
    3) variants=(corrected reference accounting);;
  esac
  for variant in "${variants[@]}"; do
    printf 'primary%d-%s cubic primary\n' "$triple" "$variant"
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
