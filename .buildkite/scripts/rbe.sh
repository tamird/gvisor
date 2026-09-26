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

if [[ "$(uname -sm)" != "Linux x86_64" ]]; then
  echo "The RBE pilot requires a Linux AMD64 worker for its coordinator and Nogo test runner." >&2
  exit 1
fi
case "${1:-}" in
  rbe-amd64|rbe-arm64) config="$1" ;;
  *) echo "Usage: $0 rbe-amd64|rbe-arm64" >&2; exit 1 ;;
esac

auth_rc=/tmp/gvisor-buildbuddy.bazelrc
if [[ ! -f "$auth_rc" || ! -r "$auth_rc" || ! -O "$auth_rc" || "$(stat -c %a "$auth_rc")" != 600 ]]; then
  echo "Provision $auth_rc owned by the Buildkite agent with mode 0600 before running this pilot." >&2
  exit 1
fi

repo_dir="$(pwd)"
version="$(cat .bazelversion)"
asset="bazel-${version}-linux-x86_64"
bazel_dir="$(mktemp -d -t gvisor-rbe-bazel.XXXXXXXX)"
bazel=("${bazel_dir}/${asset}" --nosystem_rc --nohome_rc --bazelrc="$auth_rc")
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -x "${bazel[0]}" ]]; then
    timeout --signal=KILL 30s "${bazel[@]}" shutdown || true
  fi
  rm -rf "$bazel_dir" || echo "Could not remove the temporary Bazel directory: $bazel_dir" >&2
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Keep the manifest in sync with .bazelversion using the release's .sha256
# files at https://releases.bazel.build/<version>/release/.
curl --fail --silent --show-error --location --max-time 120 \
  "https://releases.bazel.build/${version}/release/${asset}" \
  --output "${bazel_dir}/${asset}"
(
  cd "$bazel_dir"
  awk -v asset="$asset" '$2 == asset' "${repo_dir}/.buildkite/scripts/bazel.sha256" \
    | sha256sum --check --strict
)
chmod +x "${bazel[0]}"

# Keep outputs for the post-command hook to collect failures after shutdown.
"${bazel[@]}" build --config="$config" --color=no --curses=no //runsc:runsc
"${bazel[@]}" test --config="$config" --color=no --curses=no \
  --strip=never --incompatible_sandbox_hermetic_tmp=false \
  --test_output=errors --keep_going --verbose_failures \
  --build_event_json_file=.build_events.json //runsc:runsc_nogo
