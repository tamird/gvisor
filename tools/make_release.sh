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

if [[ "$#" -lt 3 ]]; then
  echo "usage: $0 <private-key> <root> <stage> [python-dir [prebuilt-path ...]]" >&2
  echo "Set NIGHTLY=true to publish the nightly packages." >&2
  exit 1
fi

set -xeo pipefail
declare -r private_key="$1" root="$2" stage="$3"
shift 3
if [[ ! -f "${private_key}" ]]; then
  echo "Missing release key: ${private_key}" >&2
  exit 1
fi
python="${stage}/python"
if (($#)); then
  [[ ! -d "$1" ]] || python="$1"
  shift
fi
declare -a required pkgs binaries sums
if (($#)); then
  # Make's prebuilt inputs can be signed without starting Bazel.
  required=("$@")
else
  mapfile -d '' -t required <"${stage}/inputs"
fi
for path in "${required[@]}"; do
  src="${stage}/${path}"
  if [[ ! -f "${src}" ]]; then echo "Missing release file: ${src}" >&2; exit 1; fi
  if [[ "${path}" == *.deb ]]; then pkgs+=("${src}"); else binaries+=("${src}"); fi
done
(("${#pkgs[@]}" && "${#binaries[@]}"))
shopt -s nullglob
binaries+=("${python}"/*.whl "${python}"/*.tar.gz)
shopt -u nullglob

# Prebuilt inputs may lack the checkfiles produced by the Bazel rule. Compute
# those once and retain the literal GNU checkfile contents for each alias.
for src in "${binaries[@]}"; do
  if (($#)) || [[ "${src%/*}" == "${python}" && "${python}" != "${stage}/python" ]]; then
    sums+=("$(cd -- "${src%/*}" && sha512sum -- "${src##*/}")")
  else
    sums+=("$(<"${src}.sha512")")
  fi
done

export DEBIAN_FRONTEND=noninteractive

# Versioned directories include wheels. PyPI serves the latest Python package.
install_raw() {
  local i src dir arch dest
  for i in "${!binaries[@]}"; do
    src="${binaries[i]}"
    dir="${src%/*}"; arch="${dir##*/}"
    if [[ "${dir}" == "${python}" ]]; then
      [[ "${2:-true}" == true ]] || continue
      arch=python
    fi
    dest="${root}/$1/${arch}"
    mkdir -p -- "${dest}"
    cp --reflink=auto --remove-destination -L -- "${src}" "${dest}/"
    printf '%s\n' "${sums[i]}" >"${dest}/${src##*/}.sha512"
  done
}

# install_apt installs an apt repository.
install_apt() {
  tools/make_apt.sh "${private_key}" "$1" "${root}" "${pkgs[@]}"
}



# If nightly, install only nightly artifacts.
if [[ "${NIGHTLY:-false}" == "true" ]]; then
  # Install the nightly release.
  # https://gvisor.dev/docs/user_guide/install/#nightly
  stamp="$(date -Idate)"
  # Nightly builds are never published to PyPI, so skip the wheels entirely.
  install_raw "nightly/latest" false
  install_raw "nightly/${stamp}" false
  install_apt "nightly"
else
  # Is it a tagged release? Build that.
  tags="$(git tag --points-at HEAD 2>/dev/null || true)"
  if ! [[ -z "${tags}" ]]; then
    # Note that a given commit can match any number of tags. We have to iterate
    # through all possible tags and produce associated artifacts.
    for tag in ${tags}; do
      # A CI cache tag is not a release.
      if [[ "$tag" == "buildkite-test-branch" ]]; then
        continue
      fi
      # A staging tag names a release that is still being built.
      if [[ "$tag" == release-*-staging ]]; then
        continue
      fi
      name=$(echo "${tag}" | cut -d'-' -f2)
      base=$(echo "${name}" | cut -d'.' -f1)
      # Install the "specific" release. This is the latest release with the
      # given date.
      # https://gvisor.dev/docs/user_guide/install/#specific-release
      install_raw "release/${base}"
      # Install the "point release".
      # https://gvisor.dev/docs/user_guide/install/#point-release
      install_raw "release/${name}"
      tools/make_python_release.sh upload-wheel "${root}/release/${name}/python"
      install_apt "${base}"

      # Install the latest release, unless a newer one exists: a point release
      # for an older date must not move users backwards. The tag itself is not
      # pushed until after the upload, so it is added to the list here.
      # https://gvisor.dev/docs/user_guide/install/#latest-release
      # A separate assignment, so that a failed lookup stops the release.
      published="$(git ls-remote --tags --refs \
        https://github.com/google/gvisor.git 'refs/tags/release-*')"
      newest="$( (echo "${published}" | cut -f2 | sed 's|^refs/tags/||' |
        awk '!/-staging$/'; echo "${tag}") | sort -V | tail -n 1)"
      if [[ "${newest}" != "${tag}" ]]; then
        echo "Not updating the latest release: ${newest} is newer than ${tag}."
        continue
      fi
      # Unlike the versioned directories above, this one is overwritten by every
      # release, so it must not accumulate version-named wheels. PyPI is the
      # canonical source for the latest Python package.
      install_raw "release/latest" false
      install_apt "release"
    done
  else
    # Otherwise, assume it is a raw master commit.
    # https://gvisor.dev/docs/user_guide/install/#head
    # HEAD builds are never published to PyPI, so skip the wheels entirely.
    install_raw "master/latest" false
    install_apt "master"
  fi
fi
