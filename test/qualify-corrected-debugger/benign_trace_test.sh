#!/bin/bash
set -eu
c=$(docker run -d --runtime corrected --user bob --cap-drop=ALL --network=none gvisor.dev/images/basic/sudo /bin/sleep 90)
trap 'timeout 8s docker rm -f "$c" >/dev/null 2>&1 || true' EXIT
test "$(docker inspect --format '{{.HostConfig.Runtime}} {{.Config.User}}' "$c")" = 'corrected bob'
timeout 20s docker exec --user bob "$c" /bin/sh -ec '
  test "$(id -un)" = bob
  test "$(id -u)" != 0
  test "$(id -gn)" = bob
  for exe in /usr/bin/strace /bin/true; do test -x "$exe"; test ! -u "$exe"; test ! -g "$exe"; done
  grep -Eq "^CapEff:[[:space:]]*0+$" /proc/self/status
  exec /usr/bin/strace -e trace=none /bin/true
' >"$TEST_TMPDIR/trace" 2>&1
cat "$TEST_TMPDIR/trace"
grep -Fq 'exited with 0' "$TEST_TMPDIR/trace"
