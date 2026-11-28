#!/bin/bash

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail
root="$TEST_SRCDIR/$TEST_WORKSPACE"
work="$TEST_TMPDIR/obsolete-upload"
mkdir -p "$work/bin"
cat > "$work/bin/git" <<'STUB'
#!/bin/bash
set -eu
[[ "$*" == 'ls-remote --exit-code origin refs/pull/123/head' ]]
printf 'lookup\n' >> "$TRACE"
if [[ "$QUERY_MODE" == malformed ]]; then
  echo unknown
else
  printf '%s\trefs/pull/123/head\n' "$QUERY_HEAD"
fi
exit "$QUERY_EXIT"
STUB
cat > "$work/bin/timeout" <<'STUB'
#!/bin/bash
set -eu
[[ "$1" == 10s ]]
shift
exec "$@"
STUB
cat > "$work/bin/buildkite-agent" <<'STUB'
#!/bin/bash
set -eu
[[ "$*" == 'build cancel' ]]
printf 'cancel\n' >> "$TRACE"
exit "$CANCEL_EXIT"
STUB
cat > "$work/bin/touch" <<'STUB'
#!/bin/bash
printf 'continue\n' >> "$TRACE"
exit 89
STUB
chmod +x "$work/bin/"*
current=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
new=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
check() {
  local name=$1 expected=$2 expected_trace=$3 status
  shift 3
  : > "$work/trace"
  set +e
  (
    export PATH="$work/bin:$PATH" TRACE="$work/trace"
    export BUILDKITE_PIPELINE_SLUG=first-pass BUILDKITE_SOURCE=webhook
    export BUILDKITE_REBUILT_FROM_BUILD_ID= BUILDKITE_PULL_REQUEST=123
    export BUILDKITE_COMMIT="$current" BUILDKITE_COMMAND='buildkite-agent pipeline upload .buildkite/first-pass.yaml'
    export QUERY_HEAD="$new" QUERY_MODE=normal QUERY_EXIT=0 CANCEL_EXIT=0
    if (($#)); then export "$@"; fi
    cd "$root"
    bash .buildkite/hooks/pre-command
  ) > "$work/$name.log" 2>&1
  status=$?
  set -e
  [[ "$status" == "$expected" ]]
  [[ "$(paste -sd, "$work/trace")" == "$expected_trace" ]]
  printf 'PASS %s exit=%s trace=%s\n' "$name" "$status" "$expected_trace"
}
check stale 1 lookup,cancel
check current 89 lookup,continue QUERY_HEAD="$current"
check unknown-ref 89 lookup,continue QUERY_EXIT=2
check unknown-timeout 89 lookup,continue QUERY_EXIT=124
check unknown-output 89 lookup,continue QUERY_MODE=malformed
check manual 89 continue BUILDKITE_SOURCE=api
check rebuild 89 continue BUILDKITE_REBUILT_FROM_BUILD_ID=previous-build
check other-pipeline 89 continue BUILDKITE_PIPELINE_SLUG=pipeline
check ordinary-job 89 continue BUILDKITE_COMMAND='make syscall-tests'
check branch-build 89 continue BUILDKITE_PULL_REQUEST=false
check symbolic-commit 89 continue BUILDKITE_COMMIT=HEAD
check cancellation-error 42 lookup,cancel CANCEL_EXIT=42
printf 'OBSOLETE_UPLOAD_CASES=12; LIVE_CALLS=0\n'
