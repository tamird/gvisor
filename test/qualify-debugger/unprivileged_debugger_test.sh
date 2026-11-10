#!/bin/bash
set -eu
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}"
image=us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/basic/sudo_x86_64@sha256:b3d0b74c37eeec266b9bd38a9b8311287fdf3db5cefa7afc1e3efe9005f366a5
result=0
for runtime in runc original; do
  id=$(docker run -d --runtime "$runtime" --user bob --cap-drop=ALL --network=none "$image" /bin/sleep 110)
  trap 'timeout 8s docker rm -f "$id" >/dev/null 2>&1 || true' EXIT
  docker inspect --format '{{.HostConfig.Runtime}} {{.Config.User}}' "$id" | tee "$out/$runtime.identity"
  test "$(cat "$out/$runtime.identity")" = "$runtime bob"
  docker exec --user bob "$id" /bin/sh -ec '
    test "$(id -un)" = bob
    test "$(id -u)" != 0
    test "$(id -gn)" = bob
    for exe in /usr/bin/strace /bin/true; do test -x "$exe"; test ! -u "$exe"; test ! -g "$exe"; done
    grep -Eq "^CapEff:[[:space:]]*0+$" /proc/self/status
    exec /usr/bin/strace -e trace=none /bin/true
  ' >"$out/$runtime.trace" 2>&1 &
  client=$!
  deadline=$((SECONDS + 15))
  while kill -0 "$client" 2>/dev/null && (( SECONDS < deadline )); do sleep .1; done
  if kill -0 "$client" 2>/dev/null; then
    echo timeout >"$out/$runtime.status"
    if test "$runtime" = original; then
      rootpath=$(sudo -n cat "$SMOKE/oci-root")
      case "$rootpath" in /run/*|/var/run/*|/tmp/gv-debugger/*) ;; *) exit 2;; esac
      timeout 6s sudo -n "$SMOKE/release/runsc" --root="$rootpath" debug --stacks "$id" >"$out/$runtime.stacks" 2>&1 || true
    fi
    kill "$client" || true
    result=1
  elif wait "$client"; then
    echo passed >"$out/$runtime.status"
    grep -Fq 'exited with 0' "$out/$runtime.trace" || result=1
  else
    echo failed >"$out/$runtime.status"
    result=1
  fi
  printf '%s %s\n' "$runtime" "$(cat "$out/$runtime.status")"
  timeout 8s docker rm -f "$id" >/dev/null 2>&1 || true
  trap - EXIT
done
exit "$result"
