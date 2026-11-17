#!/bin/bash
set -euo pipefail
: "${SOURCE:?}" "${QUAL_CONFIG:?}" "${QUAL_ARCH:?}" "${RESULT_DIR:?}"
test "$SOURCE" = c825b4c37e988240d853788a3a1dceb1ec027db4
test "$(git rev-parse HEAD)" = "$SOURCE"
case "$QUAL_ARCH:$QUAL_CONFIG" in amd64:x86_64|arm64:aarch64) ;; *) exit 2 ;; esac
mkdir -p "$RESULT_DIR"
flags=("--config=$QUAL_CONFIG" --jobs=3 --strip=never --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
status=0
for name in lint lint-cc; do
  command=(make "${make_args[@]}" "$name")
  printf '%q ' "${command[@]}" > "$RESULT_DIR/$name.command"
  printf '\n' >> "$RESULT_DIR/$name.command"
  rc=0
  "${command[@]}" 2>&1 | tee "$RESULT_DIR/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$SOURCE" "$name" "$rc" >> "$RESULT_DIR/commands.tsv"
  if test "$rc" != 0; then status=1; fi
done
exit "$status"
