#!/bin/bash
# Primary-output fixture capture only; no comparative measurements.
set -euo pipefail
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${GITHUB_RUN_ID:?}"
test "$SOURCE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$BASE" = bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa
[[ "$GITHUB_RUN_ID" =~ ^[0-9]+$ ]]
name="iperf-output-${GITHUB_RUN_ID}"
command -v ss
# Do not mistake another listener for this capture server.
if ss -ltn 'sport = :19876' | tail -n +2 | grep -q .; then exit 1; fi
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --nosystem_rc --nohome_rc' 'BAZEL_OPTIONS=--remote_executor= --remote_cache= --bes_backend=')
timeout --kill-after=10s 5m make "${make_args[@]}" pull-benchmarks_iperf 2>&1 | tee "$RESULT_DIR/image-pull.log"
image=gvisor.dev/images/benchmarks/iperf:latest
docker image inspect "$image" > "$RESULT_DIR/image.json"
id=$(docker image inspect --format '{{.Id}}' "$image")
timeout --kill-after=5s 30s docker run --rm --runtime=runc "$id" iperf --version > "$RESULT_DIR/version.txt" 2>&1
docker run --detach --name "$name" --runtime=runc --network=host "$id" iperf --server --port 19876 > "$RESULT_DIR/server-container.txt"
# Synchronize with actual listening socket, without generating transfer traffic.
for ((attempt=0; attempt<100; attempt++)); do
 if ss -ltn 'sport = :19876' | tail -n +2 | read -r line; then break; fi
 if ! docker inspect --format '{{.State.Running}}' "$name" | grep -qx true; then docker logs "$name"; exit 1; fi
 sleep .1
done
ss -ltn 'sport = :19876' | tail -n +2 | grep -q .
for spec in single:1024:1 multi:1024:4 tiny:1:1; do
 IFS=: read -r label kb parallel <<< "$spec"
 printf '%s\n' "iperf --client 127.0.0.1 --port 19876 --format K --realtime --len 128K --num ${kb}K --parallel $parallel" > "$RESULT_DIR/$label.argv.txt"
 timeout --kill-after=5s 30s docker run --rm --runtime=runc --network=host "$id" iperf --client 127.0.0.1 --port 19876 --format K --realtime --len 128K --num "${kb}K" --parallel "$parallel" > "$RESULT_DIR/$label.stdout.txt" 2> "$RESULT_DIR/$label.stderr.txt"
done
docker logs "$name" > "$RESULT_DIR/server.stdout.txt" 2> "$RESULT_DIR/server.stderr.txt"
test "$(docker image inspect --format '{{.Id}}' "$image")" = "$id"
