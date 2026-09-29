#!/bin/bash

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

# Fail before entering make_apt's installed-workflow package bootstrap. This
# container has no network; the declared image must already supply every tool.
dpkg-sig --help >/dev/null
apt-ftparchive --help >/dev/null
xz --help >/dev/null
for tool in bash gpg gpgconf dpkg file tar bzip2 zstd gzip awk grep stat cmp sha256sum sha512sum; do
  command -v "$tool"
done
test ! -e .git
test ! -e tools/make_python_release.sh

tools/make_test_key.sh /work/repo.key
artifacts=(/input/artifacts/{amd64,arm64}/*)
test "${#artifacts[@]}" -eq 6
NIGHTLY=false tools/make_release.sh /work/repo.key /work/repo "${artifacts[@]}"

verify_home=$(mktemp -d)
cleanup() {
  local status=$?
  gpgconf --homedir "$verify_home" --kill all || status=1
  rm -rf "$verify_home" || status=1
  exit "$status"
}
trap cleanup EXIT
gpg --homedir "$verify_home" --batch --import /work/repo.key
gpg --homedir "$verify_home" --armor --export > /reports/test-public-key.asc
release=/work/repo/dists/master
gpg --homedir "$verify_home" --verify "$release/Release.gpg" "$release/Release"
gpg --homedir "$verify_home" --output /work/inrelease-content --decrypt "$release/InRelease"
cmp /work/inrelease-content "$release/Release"
cp "$release/Release" "$release/Release.gpg" "$release/InRelease" /reports/

# Follow the signed metadata's digest links, including compressed indexes.
awk '/^SHA256:$/ { hashes=1; next } /^[^ ]/ { hashes=0 } hashes { print }' \
  "$release/Release" > /reports/index-sha256.txt
test -s /reports/index-sha256.txt
while read -r digest size path; do
  test "$(stat -c%s "$release/$path")" = "$size"
  (cd "$release"; printf '%s  %s\n' "$digest" "$path" | sha256sum --check --strict)
done < /reports/index-sha256.txt

for arch in amd64 arm64; do
  package=/input/artifacts/$arch/runsc.deb
  version=$(dpkg --field "$package" Version)
  signed=/work/repo/pool/$version/binary-$arch/runsc.deb
  dpkg-sig -g "--homedir $verify_home" --verify "$signed" | tee "/reports/$arch-signature.txt"
  grep -q '^GOODSIG ' "/reports/$arch-signature.txt"
  packages=$release/main/binary-$arch/Packages
  test "$(grep -c '^Package:' "$packages")" -eq 1
  grep -qx "Architecture: $arch" "$packages"
  grep -Fqx "Version: $version" "$packages"
  filename=$(awk '$1 == "Filename:" { print $2 }' "$packages")
  size=$(awk '$1 == "Size:" { print $2 }' "$packages")
  digest=$(awk '$1 == "SHA256:" { print $2 }' "$packages")
  test "/work/repo/$filename" = "$signed"
  test "$(stat -c%s "$signed")" = "$size"
  printf '%s  %s\n' "$digest" "$signed" | sha256sum --check --strict
  cp "$packages" "/reports/$arch-Packages"
  case "$arch" in
    amd64) raw_arch=x86_64 ;;
    arm64) raw_arch=aarch64 ;;
  esac
  (
    cd "/work/repo/master/latest/$raw_arch"
    sha512sum --check gvisor.tar.bz2.sha512 gvisor.tar.zstd.sha512
  )
done
echo 'Release repository: both package signatures, signed APT metadata and raw archive checksums verified.'
