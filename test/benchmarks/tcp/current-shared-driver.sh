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
git diff --exit-code "$base" HEAD -- pkg runsc tools .bazelrc MODULE.bazel
printf '%s\n' "$base" > "$out/production.txt"
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
bazel run --jobs=5 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
helpers="$RUNNER_TEMP/tcp-benchmark-helpers"
mkdir -p "$helpers" "$RUNNER_TEMP/tcp-public"
for tool in tcp_proxy nsjoin; do
  paths=$(bazel cquery --output=files "//test/benchmarks/tcp:$tool")
  mapfile -t outputs <<< "$paths"
  (("${#outputs[@]}" == 1))
  test -x "${outputs[0]}"
  ln -sf "$(realpath "${outputs[0]}")" "$helpers/$tool"
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
phase=preflight
# Preserve the original command's stdout as well as stderr: missing helpers
# are reported on stdout. This control is short and never enters the table.
run_pair "$out/preflight/default" 3 55 plain --client reno linux || :
staged="$out/preflight/staged"
if ! run_pair "$staged" 5 65 plain --client reno linux --helpers "$helpers"; then
  tail -10 "$staged/stdout" "$staged/stderr"
  exit 1
fi
python3 - "$staged/results" <<'SHORT'
import json, sys
from pathlib import Path
r = Path(sys.argv[1])
assert (r/"qdisc-before.txt").read_text().count("qdisc netem ") == 2
assert (r/"qdisc-after.txt").read_text().count("qdisc netem ") == 2
assert (r/"topology-cleanup-exit.txt").read_text().strip() == "0"
for side in ("primary", "secondary"):
    d=r/side
    for kind in ("flow","cleanup","receiver","iperf"):
        assert (d/(kind+"-exit.txt")).read_text().strip() == "0", (side,kind)
    j=json.loads((d/"receiver.json").read_text())
    assert not j.get("error") and j["end"]["sum_received"]["bytes"]>0, side
SHORT
for dev in client.1 client2.1 server.1; do
  test -s "$staged/results/$dev.pcap"
  grep -q "0 packets dropped by kernel" "$staged/results/$dev-capture.txt"
  tshark -n -r "$staged/results/$dev.pcap" -c 5 -T fields -e tcp.seq_raw | grep -q "[[:digit:]]"
done
phase=trials
printf 'mode\tcase\tprimary\tcc\tsecondary\n' > "$out/cases.tsv"
# Each controller and role occurs once per observation mode. Rotate the modes
# to avoid conflating their effect with the machine warming up over the run.
status=0
for arm in plain:reno measured:cubic measured:reno plain:cubic; do
  mode=${arm%:*}; cc=${arm#*:}; name="$cc-netstack"
  dir="$out/$mode/$name"
  printf '%s\t%s\t%s\t%s\t%s\n' "$mode" "$name" netstack "$cc" linux >> "$out/cases.tsv"
  if ! run_pair "$dir" 45 110 "$mode" --client "$cc" linux --helpers "$helpers"; then
    tail -10 "$dir/stdout" "$dir/stderr"
    status=1
  fi
done
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
