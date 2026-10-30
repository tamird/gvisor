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
before=777852bfe36452d63715f2c678e82b0f41cd760f
amended=5107630c44ad2be29c52175d1d7d26df80e6ce2b
[[ "$(git rev-parse HEAD^)" == "$amended" ]]
[[ "$(git rev-parse "$amended~2")" == "$before" ]]
git diff --exit-code "$amended" -- pkg runsc tools .bazelrc MODULE.bazel
printf '%s\n' "$before" "$amended" "$GITHUB_SHA" > "$out/production.txt"
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
printf '%s\n' "$before" "$amended" "$GITHUB_SHA" > "$RUNNER_TEMP/tcp-public/source-heads"
mapfile -t sources < <(git diff --name-only "$before" "$amended")
[[ ${#sources[@]} == 3 ]]
for variant in before amended; do
  sourcesha=$before
  [[ "$variant" == before ]] || sourcesha=$amended
  for path in "${sources[@]}"; do
    git show "$sourcesha:$path" > "$path"
  done
  git diff --exit-code "$sourcesha" -- pkg runsc tools .bazelrc MODULE.bazel
  printf "%s %s %s %s\n" "$variant" "$sourcesha" "$(git rev-parse "$sourcesha:pkg/tcpip/transport/tcp")" "$(date -u +%FT%TZ)" >> "$RUNNER_TEMP/tcp-public/sender-trees"
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
[[ "$(sha256sum "$RUNNER_TEMP/tcp-benchmark-helpers/before/tcp_proxy" | cut -d' ' -f1)" != "$(sha256sum "$RUNNER_TEMP/tcp-benchmark-helpers/amended/tcp_proxy" | cut -d' ' -f1)" ]]
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
            variant = ("before", "amended")[(position + offset) % 2]
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
