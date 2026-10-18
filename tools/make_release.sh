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

if [[ "$#" -le 2 ]]; then
  echo "usage: $0 <private-key> <root> <binaries & packages...>"
  echo "The environment variable NIGHTLY may be set to control"
  echo "whether the nightly packages are produced or not."
  exit 1
fi

set -xeo pipefail
source tools/release_repository.sh
declare -r release_key="$1" release_root="$2"
shift 2

if [[ "${NIGHTLY:-false}" == true ]]; then
  make_release_repository "$release_key" "$release_root" nightly "" "" "$@"
  exit
fi

tags="$(git tag --points-at HEAD 2>/dev/null || true)"
if [[ -z "$tags" ]]; then
  make_release_repository "$release_key" "$release_root" master "" "" "$@"
  exit
fi

tags="$(release_tags "$tags")"
if [[ -z "$tags" ]]; then
  exit
fi
# A failed lookup must stop the release, rather than promoting an older tag.
published="$(git ls-remote --tags --refs https://github.com/google/gvisor.git 'refs/tags/release-*')"
published="$(printf '%s\n' "$published" | cut -f2 | sed 's|^refs/tags/||')"
make_release_repository "$release_key" "$release_root" release "$tags" "$published" "$@"

# Do not upload Python artifacts until all repository generation and signing
# have succeeded. Publication remains the responsibility of this wrapper.
for tag in $tags; do
  name=$(echo "$tag" | cut -d'-' -f2)
  tools/make_python_release.sh upload-wheel "$release_root/release/$name/python"
done
