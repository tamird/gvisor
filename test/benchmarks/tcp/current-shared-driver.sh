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
before=5107630c44ad2be29c52175d1d7d26df80e6ce2b
amended=58df82f1bfea9f9a73459f9d54f8364b1f4a1479
[[ "$(git rev-parse HEAD^1)" == "e38a0dbdf5f717297444e4bd4036d49f45498e33" ]]
[[ "$(git rev-parse HEAD^2)" == "$amended" ]]
[[ "$(git rev-parse "$amended^")" == "$before" ]]
git diff --exit-code HEAD^1 -- test/benchmarks/tcp/{tcp_proxy.go,observation.go,nsjoin.c,BUILD,tcp_benchmark.sh}
git diff --exit-code "$before" -- pkg runsc tools .bazelrc MODULE.bazel
printf '%s\n' "$before" "$amended" "$GITHUB_SHA" > "$out/production.txt"
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark tcpdump iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
mkdir -p "$RUNNER_TEMP/tcp-public"
printf '%s\n' "$before" "$amended" "$GITHUB_SHA" > "$RUNNER_TEMP/tcp-public/source-heads"
mapfile -t sources < <(git diff --name-only "$before" "$amended")
[[ ${#sources[@]} == 2 ]]
for variant in combined with15432; do
  sourcesha=$before
  [[ "$variant" == combined ]] || sourcesha=$amended
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
[[ "$(sha256sum "$RUNNER_TEMP/tcp-benchmark-helpers/combined/tcp_proxy" | cut -d' ' -f1)" != "$(sha256sum "$RUNNER_TEMP/tcp-benchmark-helpers/with15432/tcp_proxy" | cut -d' ' -f1)" ]]
[[ "$(sha256sum "$RUNNER_TEMP/tcp-benchmark-helpers/combined/tcp_proxy" | cut -d' ' -f1)" == "c3883f31b4009523de81041bd7dfebcc0a5e49513720a9e884dc3612016d9e84" ]]
cmp "$RUNNER_TEMP/tcp-benchmark-helpers/combined/nsjoin" "$RUNNER_TEMP/tcp-benchmark-helpers/with15432/nsjoin"
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
printf '%s\n' 'b-combined cubic' 'b-with15432 cubic' > "$RUNNER_TEMP/tcp-public/schedule.txt"
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
