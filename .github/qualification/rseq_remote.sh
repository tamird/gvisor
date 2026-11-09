#!/bin/bash
set -euo pipefail
umask 077
d="$RUNNER_TEMP/rseq-native"
rc="$d/remote.bazelrc"
cleanup() {
  "$d/bazel" --nosystem_rc --nohome_rc --bazelrc=/dev/null shutdown >/dev/null 2>&1 || true
  rm -f "$rc" "$d"/*.log "$d"/*.bep "$d"/*.compact "$d"/*.graph
}
trap cleanup EXIT
: "${BUILDBUDDY_API_KEY:?missing existing fork key}"
{
  printf '%s\n' 'build --remote_executor=grpcs://remote.buildbuddy.io' 'build --remote_cache=grpcs://remote.buildbuddy.io' 'build --bes_backend=grpcs://remote.buildbuddy.io' 'build --bes_results_url=https://app.buildbuddy.io/invocation/'
  printf '%s\n' 'build --experimental_remote_downloader=grpcs://remote.buildbuddy.io --noexperimental_remote_downloader_propagate_credentials --experimental_remote_downloader_local_fallback'
  printf 'build --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
  printf 'build --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
} > "$rc"
unset BUILDBUDDY_API_KEY
clean_env=(env -i PATH="$PATH" HOME="$HOME" USER="$USER" RUNNER_TEMP="$RUNNER_TEMP" LANG=C.UTF-8)
"${clean_env[@]}" python3 -B "$d/rseq_native.py"
