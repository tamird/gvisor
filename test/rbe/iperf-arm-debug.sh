set -euo pipefail
r="$RUNNER_TEMP/iperf-diagnostic"; mkdir -p "$r"
exec > >(tee "$r/diagnostic.log") 2>&1
test "$(uname -m)" = aarch64
uname -a
command -v gdb || { sudo apt-get update -qq; sudo apt-get install -y -qq gdb; }
image=gvisor.dev/images/benchmarks/iperf:latest
work="$RUNNER_TEMP/iperf-core"; mkdir -p "$work"; chmod 1777 "$work"
prior_pattern=$(sysctl -n kernel.core_pattern)
cleanup() { docker logs diagnostic-server >"$r/server.log" 2>&1 || :; sudo sysctl -qw "kernel.core_pattern=$prior_pattern" || :; docker rm -f diagnostic-client diagnostic-server >/dev/null 2>&1 || :; docker network rm diagnose >/dev/null 2>&1 || :; }; trap cleanup EXIT
sudo sysctl -w kernel.core_pattern=/tmp/iperf-core.%p
docker network create diagnose
docker run -d --name diagnostic-server --network diagnose -p 127.0.0.1:35201:5201 --user 65532:65532 "$image" iperf3 -s --forceflush
for i in {1..20}; do docker logs diagnostic-server 2>&1 | grep -q 'Server listening' && break; sleep 1; done
docker logs diagnostic-server 2>&1 | grep 'Server listening'
docker run --name diagnostic-client --network diagnose --user 65532:65532 --ulimit core=-1 --mount "type=bind,source=$work,target=/tmp" -d "$image" iperf3 -c diagnostic-server -t 1 --connect-timeout 5000 --json
timeout 35s docker wait diagnostic-client >"$r/exit"
docker logs diagnostic-client >"$r/stdout.json" 2>"$r/stderr"
docker cp diagnostic-client:/usr/bin/iperf3 "$RUNNER_TEMP/iperf3"
chmod a+rx "$RUNNER_TEMP/iperf3"; sha256sum "$RUNNER_TEMP/iperf3" >"$r/executable"
grep -F a378a7268f67bcb6f4c789ec71fabe475d12df235e2279d0a02c7d74a3cba17c "$r/executable"
args=(-nx -batch -ex 'set debuginfod enabled off' -ex 'set pagination off')
analysis=(-ex 'info program' -ex 'p $_siginfo.si_signo' -ex 'bt 20' -ex 'x/13i $pc-20' -ex 'info symbol $pc' -ex 'info registers pc lr sp')
shopt -s nullglob
cores=("$work"/iperf-core.*)
if (( ${#cores[@]} )); then
  sudo gdb "${args[@]}" "${analysis[@]}" "$RUNNER_TEMP/iperf3" "${cores[0]}" >"$r/backtrace" 2>&1
else
  cp "$RUNNER_TEMP/iperf3" "$work/iperf3"; chmod a+rx "$work/iperf3"
  sudo setpriv --reuid=65532 --regid=65532 --clear-groups gdb "${args[@]}" -ex run "${analysis[@]}" --args "$work/iperf3" -c 127.0.0.1 -p 35201 -t 1 --connect-timeout 5000 --json >"$r/backtrace" 2>&1
fi
cat "$r/backtrace"
grep -E 'SIG(TRAP|ILL|ABRT|SEGV|BUS)|Trace/breakpoint trap' "$r/backtrace"
grep -F '#0' "$r/backtrace"
grep -F '=>' "$r/backtrace"
echo "Confirmed native fault and instruction; product remains blocked" >"$r/verdict"
