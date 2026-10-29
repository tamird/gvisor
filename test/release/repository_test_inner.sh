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
artifacts=(/input/artifacts/{x86_64,aarch64,python}/*)
test "${#artifacts[@]}" -eq 8
for arch in x86_64 aarch64; do
  input=/input/artifacts/$arch
  case "$arch" in
    x86_64) cpu=amd64; machine=62 ;;
    aarch64) cpu=arm64; machine=183 ;;
  esac
  test "$(dpkg-deb -f "$input/runsc.deb" Architecture)" = "$cpu"
  dpkg-deb --fsys-tarfile "$input/runsc.deb" | tar -xOf - ./usr/bin/runsc > "/work/$cpu-runsc"
  test "$(od -An -tu2 -j18 -N2 "/work/$cpu-runsc" | tr -d ' ')" = "$machine"
  tar -xjOf "$input/gvisor.tar.bz2" runsc | cmp - "/work/$cpu-runsc"
  tar --zstd -xOf "$input/gvisor.tar.zstd" runsc | cmp - "/work/$cpu-runsc"
  sha256sum "/work/$cpu-runsc" >> /reports/package-binaries.sha256
done
NIGHTLY=false tools/make_release.sh /work/repo.key /work/repo "${artifacts[@]}"
NIGHTLY=true tools/make_release.sh /work/repo.key /work/repo "${artifacts[@]}"

# Exercise the same generator with declared tag inventories. Neither Git nor
# the uploader is present in this container.
source tools/release_repository.sh
make_release_repository /work/repo.key /work/repo release \
  release-20261018.1 $'release-20261017.7\nrelease-20261019.0-staging' "${artifacts[@]}"

# The older point release uses identical inputs, so byte comparisons alone
# would miss an erroneous overwrite of latest. Require its mtimes to survive.
latest_files=(/work/repo/release/latest/{x86_64,aarch64}/*
  /work/repo/dists/release/{Release,Release.gpg,InRelease})
touch -d @946684800 "${latest_files[@]}"
make_release_repository /work/repo.key /work/repo release \
  release-20261017.8 release-20261018.1 "${artifacts[@]}"
for file in "${latest_files[@]}"; do
  test "$(stat -c%Y "$file")" -eq 946684800
done
make_release_repository /work/repo.key /work/ignored release \
  $'buildkite-test-branch\nrelease-20261019.0-staging' "" "${artifacts[@]}"
test ! -e /work/ignored

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
for channel in master nightly release 20261018 20261017; do
  reports=/reports/$channel
  mkdir -p "$reports"
  case "$channel" in
    master|release) raw_roots=(/work/repo/$channel/latest) ;;
    nightly)
      # Use the generated date so crossing midnight cannot invalidate the check.
      dated=(/work/repo/nightly/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
      test "${#dated[@]}" -eq 1
      test -d "${dated[0]}"
      raw_roots=(/work/repo/nightly/latest "${dated[0]}")
      ;;
    20261018) raw_roots=(/work/repo/release/20261018{,.1}) ;;
    20261017) raw_roots=(/work/repo/release/20261017{,.8}) ;;
  esac
  release=/work/repo/dists/$channel
  gpg --homedir "$verify_home" --verify "$release/Release.gpg" "$release/Release"
  gpg --homedir "$verify_home" --output "/work/$channel-inrelease-content" \
    --decrypt "$release/InRelease"
  cmp "/work/$channel-inrelease-content" "$release/Release"
  cp "$release/Release" "$release/Release.gpg" "$release/InRelease" "$reports/"

  # Follow the signed metadata's digest links, including compressed indexes.
  awk '/^SHA256:$/ { hashes=1; next } /^[^ ]/ { hashes=0 } hashes { print }' \
    "$release/Release" > "$reports/index-sha256.txt"
  test -s "$reports/index-sha256.txt"
  while read -r digest size path; do
    test "$(stat -c%s "$release/$path")" = "$size"
    (cd "$release"; printf '%s  %s\n' "$digest" "$path" | sha256sum --check --strict)
  done < "$reports/index-sha256.txt"

  for arch in amd64 arm64; do
    case "$arch" in
      amd64) raw_arch=x86_64 ;;
      arm64) raw_arch=aarch64 ;;
    esac
    package=/input/artifacts/$raw_arch/runsc.deb
    version=$(dpkg --field "$package" Version)
    signed=/work/repo/pool/$version/binary-$arch/runsc.deb
    GNUPGHOME="$verify_home" dpkg-sig --verify "$signed" | tee "$reports/$arch-signature.txt"
    grep -q '^GOODSIG ' "$reports/$arch-signature.txt"
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
    cp "$packages" "$reports/$arch-Packages"
    for raw_root in "${raw_roots[@]}"; do
      (
        cd "$raw_root/$raw_arch"
        sha512sum --check gvisor.tar.bz2.sha512 gvisor.tar.zstd.sha512
        cmp gvisor.tar.bz2 "/input/artifacts/$raw_arch/gvisor.tar.bz2"
        cmp gvisor.tar.zstd "/input/artifacts/$raw_arch/gvisor.tar.zstd"
      )
    done
  done
  for raw_root in "${raw_roots[@]}"; do
    case "$channel" in
      20261018|20261017)
        python_files=("$raw_root"/python/*.whl "$raw_root"/python/*.tar.gz)
        test "${#python_files[@]}" -eq 2
        for file in "${python_files[@]}"; do
          cmp "$file" "/input/artifacts/python/$(basename "$file")"
          (cd "$raw_root/python"; sha512sum --check "$(basename "$file").sha512")
        done
        ;;
      *) test ! -e "$raw_root/python" ;;
    esac
  done
done
echo 'Master, nightly and tagged repositories: package signatures, signed APT metadata and source-built archives verified.'
