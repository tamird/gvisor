#!/bin/bash
set -eu -o pipefail

cd /workspace
test "$(cat .bazelversion)" = 9.3.1
test ! -e .git
test ! -L .git
test ! -e /var/run/docker.sock
test ! -e /root/.gitconfig
test "$(bazel --version)" = 'bazel 9.3.1'
for tool in git cc gcc clang ld make docker; do
  if command -v "$tool" >/dev/null; then exit 1; fi
done

# Only the disposable Jammy container's filesystem is changed here.
# A removed command tests PATH search as well as absolute /bin/grep calls.
if [[ "$MODE" == absent ]]; then
  rm -f -- /bin/grep /usr/bin/grep
  hash -r
  test ! -e /usr/bin/grep
  test ! -e /bin/grep
  if command -v grep >/dev/null; then exit 1; fi
else
  test "$MODE" == original
  test -x /usr/bin/grep
fi
printf '%s\n' "$MODE" > "/audit/$MODE/mode"

for cpu in arm64 amd64; do
  config=x86_64
  [[ "$cpu" != arm64 ]] || config=aarch64
  out="/audit/$MODE/$cpu"
  trace=()
  if [[ "$MODE" == original ]]; then
    # Keep only grep executions in this trace. The outer verifier projects a
    # bounded list of public argument shapes and discards the private trace.
    trace=(/lib64/ld-linux-x86-64.so.2 --library-path /observer/usr/lib/x86_64-linux-gnu
           /observer/usr/bin/strace --seccomp-bpf -f -qq -z -s 192 -e trace=execve,execveat
           -P /bin/grep -P /usr/bin/grep -o "$out/grep.trace")
  fi
  rc=0
  "${trace[@]}" /usr/local/bin/bazel --batch --nosystem_rc --nohome_rc build \
    "--config=$config" --config=release_version "--embed_label=$RELEASE_LABEL" -c opt \
    --jobs=4 --remote_cache= --remote_executor= --disk_cache= \
    "--build_event_json_file=$out/build.bep" --noshow_progress --color=no \
    --curses=no --verbose_failures //:release > "$out/build.log" 2>&1 || rc=$?
  printf '%s\n' "$rc" > "$out/build.exit"
  if [[ "$rc" == 0 ]]; then
    cp -RL bazel-bin/release "$out/files"
  fi
done
