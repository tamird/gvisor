#!/usr/bin/env bash
set -euo pipefail
runtime=$1
monitor=$(command -v busybox)
for iface in any lo; do
  cid=$(docker run -d --runtime="$runtime" --cap-add NET_RAW \
    --mount "type=bind,src=$monitor,dst=/probe/busybox,readonly" \
    "$TAG" tcpdump -l -nn -i "$iface" port 9999)
  for ((i=0; i<30; i++)); do
    if [[ $(docker inspect --format '{{.State.Running}}' "$cid") != true ]]; then break; fi
    if docker logs "$cid" 2>&1 | grep -q 'listening on'; then break; fi
    sleep 1
  done
  printf 'BEGIN %s %s\n' "$runtime" "$iface"
  docker inspect --format 'startup running={{.State.Running}} exit={{.State.ExitCode}} error={{.State.Error}}' "$cid"
  docker logs "$cid" 2>&1 | tail -15
  if [[ $(docker inspect --format '{{.State.Running}}' "$cid") == true ]]; then
    docker exec "$cid" /probe/busybox cat /proc/1/status | grep -E '^(Uid|Gid):'
    docker exec "$cid" tcpdump_sender
    for ((i=0; i<10; i++)); do
      if docker logs "$cid" 2>&1 | grep -q 'UDP, length 8'; then break; fi
      sleep 1
    done
    docker stop "$cid" >/dev/null
  fi
  docker logs "$cid" 2>&1 | tail -25
  docker inspect --format 'final exit={{.State.ExitCode}} error={{.State.Error}}' "$cid"
  docker rm -f "$cid" >/dev/null
  printf 'END %s %s\n' "$runtime" "$iface"
done
