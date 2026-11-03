#!/bin/sh
set -eu
# The Sentry has --network=none. This empty in-sandbox bridge supplies the default
# route sought by start-dockerd.sh and has no uplink.
step() { echo "DIND_STEP $1"; }
step isolated-route
ip link add name eth0 type bridge
ip link set dev eth0 mtu 1460 up
ip addr add 192.0.2.2/24 dev eth0
ip route add default via 192.0.2.1 dev eth0
ip route show default | grep -q 'dev eth0'
echo 1 > /proc/sys/net/ipv4/ip_forward
test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1
ip link show dev eth0 | grep -q "mtu 1460"
step snat
fw=iptables
if command -v iptables-legacy >/dev/null; then fw=iptables-legacy; fi
for proto in tcp udp; do
  if "$fw" -t nat -A POSTROUTING -o eth0 -p "$proto" -j SNAT --to-source 192.0.2.2 &&
      "$fw" -t nat -C POSTROUTING -o eth0 -p "$proto" -j SNAT --to-source 192.0.2.2; then
    echo "DIND_SNAT $proto configured $fw"
  else
    echo "DIND_SNAT $proto unavailable $fw"
  fi
done
step daemon
export DOCKER_HOST=unix:///var/run/docker.sock DOCKER_TLS_CERTDIR=
dockerd-entrypoint.sh dockerd --host="$DOCKER_HOST" --iptables=false --ip6tables=false --mtu=1460 >/tmp/dockerd.log 2>&1 &
pid=$!
finish() {
  rc=$?
  if [ "$rc" != 0 ]; then tail -c 10000 /tmp/dockerd.log; fi
  kill "$pid" 2>/dev/null || :
  exit "$rc"
}
trap finish EXIT
for i in $(seq 1 75); do
  if docker info >/dev/null 2>&1; then break; fi
  kill -0 "$pid" || exit 71
  sleep 1
done
test "$(docker version --format '{{.Server.Version}}')" = 27.5.1
echo "DIND_DAEMON 27.5.1 $(docker info --format '{{.Driver}}')"
step load-python
docker load -i /qa/python-image.tar
test "$(docker image inspect --format '{{.Id}}' python:3.12-alpine)" = sha256:38fd5a425aa93b06d367e2e47e2da6a9bc0cabdaf5788fb1707d143fd1416e97
step network
docker network create t1
test "$(docker network inspect --format '{{.Driver}}' t1)" = bridge
echo "DIND_NETWORK $(docker network inspect --format '{{.Id}} {{range .IPAM.Config}}{{.Subnet}}{{end}}' t1)"
for mode in client-first server-first; do
  step "$mode"
  docker rm -f srv >/dev/null 2>&1 || :
  docker run --pull=never -d --name srv --network t1 -e MODE="$mode" -v /qa/dind_socket.py:/s.py:ro python:3.12-alpine python /s.py
  sleep 2
  test "$(docker inspect -f '{{.State.Running}}' srv)" = true
  echo "DIND_SERVER $mode $(docker inspect -f '{{(index .NetworkSettings.Networks "t1").IPAddress}}' srv)"
  timeout 280 docker run --pull=never --rm --network t1 -v /qa/dind_socket.py:/c.py:ro python:3.12-alpine python /c.py "$mode" 2000
done
step finished
