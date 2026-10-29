#!/bin/bash

# Copyright 2018 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Repository generation is separate from Git discovery and publication so the
# same layout can be built from declared inputs without network access.

# release_tags omits CI markers and tags for releases still being built.
release_tags() {
  local tag
  for tag in $1; do
    # LINT.IfChange
    if [[ "$tag" == "buildkite-test-branch" ]]; then
      continue
    fi
    # LINT.ThenChange(../.buildkite/hooks/pre-command)
    if [[ "$tag" == release-*-staging ]]; then
      continue
    fi
    printf '%s\n' "$tag"
  done
}

# install_raw installs raw artifacts.
#
# Usage: install_raw <dest> [include-python]
#
# Wheels and sdists carry their version in their filename, so unlike the
# architecture-specific tarballs they never overwrite the previous build's
# files. Pass "false" for <include-python> to keep them out of directories that
# are meant to be a fixed-size pointer to the most recent build; otherwise
# those directories grow without bound.
install_raw() {
  local -r dest="$1"
  local -r include_python="${2:-true}"
  local binary
  for binary in "${binaries[@]}"; do
    local arch dir name
    case "${binary}" in
      *.whl|*.tar.gz)
        arch="python"
        ;;
      *)
        dir=${binary%/*}
        case "${dir##*/}" in
          x86_64|amd64) arch=x86_64 ;;
          aarch64|arm64) arch=aarch64 ;;
          *) echo "unknown architecture directory for ${binary}" >&2; return 1 ;;
        esac
        ;;
    esac
    if [[ "${arch}" == "python" ]] && [[ "${include_python}" != "true" ]]; then
      continue
    fi
    name=$(basename "${binary}")
    mkdir -p "${root}/${dest}/${arch}"
    cp -f "${binary}" "${root}/${dest}/${arch}"
    (cd "${root}/${dest}/${arch}" && sha512sum "${name}" >"${name}.sha512")
  done
}

# install_apt installs an apt repository.
install_apt() {
  tools/make_apt.sh "${private_key}" "$1" "${root}" "${pkgs[@]}"
}

# make_release_repository <key> <root> <channel> <tags> <published-tags> <artifacts...>
#
# Tag inventories contain newline-separated names. The private key, root and
# artifact arrays are local to this call and shared with the install helpers.
make_release_repository() {
  local -r private_key="$1" root="$2" channel="$3" tags="$4" published="$5"
  shift 5
  local -a binaries=() pkgs=()
  local arg tag name base newest stamp selected
  for arg in "$@"; do
    if [[ "$arg" == *.deb || "$arg" == *.changes ]]; then
      pkgs+=("$arg")
    else
      binaries+=("$arg")
    fi
  done
  export DEBIAN_FRONTEND=noninteractive

  case "$channel" in
    master)
      install_raw master/latest false
      install_apt master
      ;;
    nightly)
      stamp="$(date -Idate)"
      install_raw nightly/latest false
      install_raw "nightly/$stamp" false
      install_apt nightly
      ;;
    release)
      selected="$(release_tags "$tags")"
      # Include all unpublished tags on this commit so iteration order cannot
      # promote an older point release over the newest one.
      newest="$(printf '%s\n' "$published" "$selected" | awk '!/-staging$/' | sort -V | tail -n 1)"
      for tag in $selected; do
        name=$(echo "$tag" | cut -d'-' -f2)
        base=$(echo "$name" | cut -d'.' -f1)
        # Retain both the latest point release for this date and this revision.
        install_raw "release/$base"
        install_raw "release/$name"
        install_apt "$base"

        # An older date's point release must not move latest users backwards.
        if [[ "$newest" != "$tag" ]]; then
          echo "Not updating the latest release: $newest is newer than $tag."
          continue
        fi
        install_raw release/latest false
        install_apt release
      done
      ;;
    *)
      printf 'Unknown release channel: %s\n' "$channel" >&2
      return 1
      ;;
  esac
}
