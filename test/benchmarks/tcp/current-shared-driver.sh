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
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iproute2 iperf3 ethtool bc kmod jq iputils-ping
sudo -n modprobe sch_netem
uname -a > "$out/worker.txt"
phase=build
bazel run --jobs=5 --script_path="$RUNNER_TEMP/start-tcp-trial" //test/benchmarks/tcp:tcp_benchmark
phase=trials
printf 'mode\tcase\tprimary\tcc\tsecondary\n' > "$out/cases.tsv"
# Each controller and role occurs once per observation mode. Rotate the modes
# to avoid conflating their effect with the machine warming up over the run.
cases=(reno-netstack cubic-linux cubic-netstack reno-linux)
modes=(plain historical measured)
status=0
for round in 0 1 2; do
  for pos in 0 1 2 3; do
    name=${cases[$(( (pos+round) % 4 ))]}
    cc=${name%-*}; primary=${name#*-}
    idx=0
    for i in 0 1 2 3; do if [[ ${cases[i]} == "$name" ]]; then idx=$i; fi; done
    mode=${modes[$(( (idx+round) % 3 ))]}
    flag=--client; other=linux
    if [[ $primary == linux ]]; then flag=--linux-client; other=netstack; fi
    dir="$out/$mode/$name"
    mkdir -p "$dir"
    printf '%s\t%s\t%s\t%s\t%s\n' "$mode" "$name" "$primary" "$cc" "$other" >> "$out/cases.tsv"
    ec=0
    sudo -n timeout --signal=INT --kill-after=15s 180s "$RUNNER_TEMP/start-tcp-trial" --sender-observation "$mode" "$flag" --no-user-ns --congestion-control "$cc" --second-client "$other" --second-congestion-control "$cc" --second-start-delay 0 --latency-probe --output-dir "$dir/results" --ideal --mtu 1500 --latency 100 --rate 20 --queue-packets 100 --duration 120 --num-client-threads 1 --sack --disable-linux-gso --disable-linux-gro > "$dir/stdout" 2> "$dir/stderr" || ec=$?
    sudo -n chown -hR "$(id -u):$(id -g)" "$dir"
    echo "$ec" > "$dir/exit.txt"
    if (( ec != 0 )); then
      tail -20 "$dir/stderr"
      status=1
      # A systematic setup failure is not worth 11 repetitions; retain the
      # missing-case verdict for every later arm.
      if [[ ! -e $dir/results/qdisc-before.txt ]]; then exit 1; fi
    fi
  done
done
date -u +%FT%TZ > "$out/trials-finished.txt"
exit "$status"
