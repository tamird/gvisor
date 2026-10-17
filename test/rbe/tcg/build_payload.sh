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

# Prepare the existing /work/payload layout once, outside the emulated CPU.
set -euo pipefail
tar_tool="$(realpath "$1")"
host_archive="$(realpath "$2")"
payload_archive="$(realpath "$3")"
output="$(realpath -m "$4")"
# shellcheck disable=SC1090
source "$5"
payload="$6"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir -p "$staging/host" "$staging/work/payload"
"$tar_tool" -xf "$host_archive" -C "$staging/host" --no-same-owner
"$tar_tool" -xpf "$payload_archive" -C "$staging/work/payload" --no-same-owner
host="$staging/host"
if [[ ! -x "$staging/work/payload/$payload" ]]; then
  printf 'Payload is missing or not executable: %s\n' "$payload" >&2
  exit 1
fi
# Cache only payload data and filesystem headroom. The launcher grows its
# private copy to the owner's existing scratch capacity before booting.
read -r bytes _ < <(host_tool usr/bin/du --apparent-size --block-size=1 -s "$staging/work")
kib=$(( (bytes * 2 + 1023) / 1024 + 64 * 1024 ))
# Match the 4-KiB filesystem blocks of the original full-size scratch disk;
# mke2fs otherwise chooses smaller blocks for a compact template.
build_ext4 "$staging/work" "$output" "${kib}K" gvisor-work -b 4096
