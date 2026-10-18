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

# Prepare the existing /work/payload layout outside the guest CPU.
set -euo pipefail
format="$1"
if [[ "$format" != --runfiles ]]; then tar_tool="$(realpath "$format")"; fi
host_input="$(realpath "$2")"
payload_input="$(realpath "$3")"
output="$(realpath -m "$4")"
# shellcheck disable=SC1090
source "$5"
payload="$6"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
mkdir -p "$staging/work/payload"
if [[ -d "$host_input" ]]; then
  # Local KVM tests share the already-extracted declared tool closure.
  host="$host_input"
else
  mkdir "$staging/host"
  "$tar_tool" -xf "$host_input" -C "$staging/host" --no-same-owner
  host="$staging/host"
fi
if [[ "$format" == --runfiles ]]; then
  # The owning rule emits calls using only declared, runfiles-relative sources
  # and checked destinations. Copies never change the shared Bazel inputs.
  stage_payload() {
    local source="$1" destination="$staging/work/payload/$2" mode="$3" kind="$4" entry
    host_tool bin/mkdir -p -- "${destination%/*}"
    if [[ "$kind" == directory ]]; then
      [[ -d "$source" ]]
      host_tool bin/cp -rL --reflink=auto --sparse=always -- "$source" "$destination"
      # Match the payload's executable file modes and searchable directories;
      # timestamps are not part of the guest execution contract.
      host_tool bin/chmod 0755 "$destination"
      shopt -s dotglob globstar nullglob
      for entry in "$destination"/**; do
        if [[ -d "$entry" ]]; then
          host_tool bin/chmod 0755 "$entry"
        else
          host_tool bin/chmod "$mode" "$entry"
        fi
      done
    else
      [[ -f "$source" ]]
      host_tool bin/cp -L --reflink=auto --sparse=always -- "$source" "$destination"
      host_tool bin/chmod "$mode" "$destination"
    fi
  }
  # shellcheck disable=SC1090
  source "$payload_input"
else
  "$tar_tool" -xpf "$payload_input" -C "$staging/work/payload" --no-same-owner
fi
if [[ ! -x "$staging/work/payload/$payload" ]]; then
  printf 'Payload is missing or not executable: %s\n' "$payload" >&2
  exit 1
fi
# Keep the image limited to payload data and filesystem headroom. The launcher
# grows its private disk to the owner's existing scratch capacity before booting.
read -r bytes _ < <(host_tool usr/bin/du --apparent-size --block-size=1 -s "$staging/work")
kib=$(( (bytes * 2 + 1023) / 1024 + 64 * 1024 ))
# Match the 4-KiB filesystem blocks of the original full-size scratch disk;
# mke2fs otherwise chooses smaller blocks for a compact template.
build_ext4 "$staging/work" "$output" "${kib}K" gvisor-work -b 4096
