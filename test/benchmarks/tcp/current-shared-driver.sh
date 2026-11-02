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
base=aa767e4c43d011121271551fe24da501a56f729d
original=6489fd4c9dc4d093c3adcc7d4412867da2e89a62
source=a89de7271af9ded5f5112651e0d504b58b08c116
public="$RUNNER_TEMP/tcp-public"
mkdir -p "$public"
[[ "$(git rev-parse HEAD^^)" == "$base" ]]
git fetch --no-tags --depth=1 origin "$original"
[[ "$(git diff --name-only "$base" "$source")" == "$(printf '%s\n' pkg/tcpip/transport/tcp/{rack,snd}.go)" ]]
expected=(.github/workflows/build.yml pkg/tcpip/transport/tcp/{rack,snd}.go test/benchmarks/tcp/current-shared-{driver.sh,report.py})
[[ "$(git diff --name-only "$original" HEAD)" == "$(printf '%s\n' "${expected[@]}")" ]]
git diff --no-ext-diff "$base" "$source" -- pkg/tcpip/transport/tcp > "$public/observer.patch"
git diff --no-ext-diff "$original" HEAD -- .github/workflows test > "$public/collector.patch"
git diff --raw --no-abbrev "$base" HEAD > "$public/all-changed-blobs.txt"
printf '%s\n' "$base" "$original" "$source" "$GITHUB_SHA" > "$public/lineage.txt"

[[ "$(git rev-parse HEAD^)" == "$source" ]]
git diff --exit-code "$source" -- pkg runsc tools .bazelrc MODULE.bazel
printf '%s\n' "$source" "$GITHUB_SHA" > "$out/production.txt"
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
printf '%s\n' "$source" "$GITHUB_SHA" > "$RUNNER_TEMP/tcp-public/source-heads"
printf "%s %s\n" "$source" "$(git rev-parse "$source:pkg/tcpip/transport/tcp")" > "$RUNNER_TEMP/tcp-public/sender-trees"
bazel run --jobs=5 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
helpers="$RUNNER_TEMP/tcp-benchmark-helpers/shared"
mkdir -p "$helpers"
for tool in tcp_proxy nsjoin; do
  paths=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
  mapfile -t outputs <<< "$paths"
  (("${#outputs[@]}" == 1))
  test -x "${outputs[0]}"
  cp "${outputs[0]}" "$helpers/$tool"
  sha256sum "$helpers/$tool" >> "$RUNNER_TEMP/tcp-public/helpers.sha256"
done
python3 - "$helpers/tcp_proxy" <<'PYTHON'
from pathlib import Path
import sys

binary=Path(sys.argv[1]).read_bytes()
assert all(x in binary for x in (b"WANLOSS %s", b"wanLossDecision"))
PYTHON
cp "$out/worker.txt" "$RUNNER_TEMP/tcp-public/worker.txt"
run_pair() {
  local dir=$1 duration=$2 limit=$3 mode=$4 flag=$5 cc=$6 other=$7
  shift 7
  mkdir -p "$dir"
  local ec=0
  sudo -n env TCP_FIRST_FLOW="$first" timeout --signal=INT --kill-after=15s "${limit}s" "$RUNNER_TEMP/start-tcp-trial" --sender-observation "$mode" "$flag" --no-user-ns --congestion-control "$cc" --second-client "$other" --second-congestion-control "$cc" --second-start-delay 0 --latency-probe --output-dir "$dir/results" --ideal --mtu 1500 --latency 100 --rate 20 --queue-packets 100 --duration "$duration" --num-client-threads 1 --sack --disable-linux-gso --disable-linux-gro "$@" > "$dir/stdout" 2> "$dir/stderr" || ec=$?
  sudo -n chown -hR "$(id -u):$(id -g)" "$dir"
  echo "$ec" > "$dir/exit.txt"
  return "$ec"
}
phase=trials
status=0
python3 - "$RUNNER_TEMP/tcp-public/schedule.txt" <<'PYTHON'
import sys
from pathlib import Path

# Two matched gVisor-first pairs, counterbalancing the sole changed setting.
rows = ["primary1-on cubic primary", "primary1-off cubic primary",
        "primary2-off cubic primary", "primary2-on cubic primary"]
Path(sys.argv[1]).write_text("\n".join(rows) + "\n")
PYTHON
while read -r arm cc first; do
  variant=${arm#*-}
  dir="$out/$arm/measured/$cc-netstack"
  mkdir -p "$dir"
  date -u +%FT%TZ > "$dir/start"
  recovery=--rack
  [[ "$variant" == on ]] || recovery=--no-rack
  if ! run_pair "$dir" 45 110 measured --client "$cc" linux --helpers "$helpers" "$recovery"; then
    tail -10 "$dir/stdout" "$dir/stderr"
    status=1
  fi
  date -u +%FT%TZ > "$dir/finish"
done < "$RUNNER_TEMP/tcp-public/schedule.txt"
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
