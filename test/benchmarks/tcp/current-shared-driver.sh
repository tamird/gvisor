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
stock=aa5305e99cc2501aede44cac257338d954e7d7dc
proposed=715df78ea05a1b292331a746d50ddeee0690403d
[[ "$(git rev-parse "$proposed^")" == "$stock" ]]
git diff --exit-code "$proposed" HEAD -- pkg runsc tools .bazelrc MODULE.bazel
printf '%s\n' "$stock" "$proposed" "$GITHUB_SHA" > "$out/production.txt"
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
printf '%s\n' "$stock" "$proposed" "$GITHUB_SHA" > "$RUNNER_TEMP/tcp-public/source-heads"
mapfile -t sources < <(git diff --name-only "$stock" "$proposed")
[[ ${#sources[@]} == 4 ]]
for variant in stock proposed; do
  sourcesha=$stock
  [[ "$variant" == stock ]] || sourcesha=$proposed
  for path in "${sources[@]}"; do
    git show "$sourcesha:$path" > "$path"
  done
  git diff --exit-code "$sourcesha" -- pkg runsc tools .bazelrc MODULE.bazel
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
python3 - "$RUNNER_TEMP/tcp-public/schedule.txt" <<'PYTHON'
import random
import sys
from pathlib import Path
seed = 16738
rng = random.Random(seed)
ccs = ["reno", "cubic"]
rng.shuffle(ccs)
rows = []
for pair in ("a", "b"):
    order = ccs if pair == "a" else list(reversed(ccs))
    for position in range(2):
        for cc in order:
            offset = (cc == "cubic") ^ (pair == "b")
            variant = ("stock", "proposed")[(position + offset) % 2]
            rows.append(f"{pair}-{variant} {cc}")
assert len(set(rows)) == 8
Path(sys.argv[1]).write_text("\n".join(rows) + "\n")
PYTHON
while read -r arm cc; do
  variant=${arm#*-}
  dir="$out/$arm/measured/$cc-netstack"
  mkdir -p "$dir"
  date -u +%FT%TZ > "$dir/start"
  if ! run_pair "$dir" 45 110 measured --client "$cc" linux --helpers "$RUNNER_TEMP/tcp-benchmark-helpers/$variant"; then
    tail -10 "$dir/stdout" "$dir/stderr"
    status=1
  fi
  date -u +%FT%TZ > "$dir/finish"
done < "$RUNNER_TEMP/tcp-public/schedule.txt"
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
