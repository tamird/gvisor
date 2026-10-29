#!/bin/bash
set -euo pipefail
export VERSION_TEMP=$1
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
export GNUPGHOME=$VERSION_TEMP/keyring
d=$VERSION_TEMP
mkdir -m700 "$GNUPGHOME"
mkdir -p "$d/native"
out=$d/result
tag=release-20261029.0
baz="$PWD/test/rbe/version_label/bazel.sh"
fixture=test/rbe/version_label
finish() {
  status=$?
  set +e
  python3 "$fixture/assembly.py" "$d" "$status"
  summary_status=$?
  chmod -R a+rX "$out"
  if ((status)); then exit "$status"; fi
  exit "$summary_status"
}
trap finish EXIT
# A public release tag would direct the publisher to remote Git and PyPI.
# On the untagged private commit it writes the local master tree and APT pool.
[[ -z $(git tag --points-at HEAD) ]]
record() {
  local stage=$1; shift
  printf '%s %s\n' "$stage" "$(date -u --iso-8601=seconds)" >> "$out/stages.txt"
  "$@" >"$d/$stage.log" 2>&1
}
record prior-opt "$baz" build -c opt //tools/bazeldefs:version
opt=$(readlink bazel-bin)
[[ $opt == *-opt/bin ]]
record fastbuild "$baz" build -c fastbuild //sandboxexec/sandbox/python:sandbox_py
fast=$(readlink bazel-bin)
[[ $fast == *-fastbuild/bin && $opt != "$fast" && ! -e bazel-bin/debian/release-artifacts ]]
printf '%s\n%s\n' "$opt" "$fast" > "$out/bin-links.txt"
# Exactly as in .buildkite/release.yaml: Make selects the production artifact
# root itself. Only the disposable local key, publisher and GNU RBE differ.
record release make DOCKER_BUILD=false "BAZEL=$baz" BAZEL_OPTIONS=--compilation_mode=opt \
  RELEASE_ROOT="$d/published" RELEASE_KEY="$d/repo.key" RELEASE_VERSION="$tag" release
final=$(readlink bazel-bin)
[[ $final == *-opt/bin && $final != "$fast" ]]
printf '%s\n' "$final" >> "$out/bin-links.txt"
staged=bazel-bin/debian/release-artifacts
record packages python3 "$fixture/packages.py" "$staged" "$tag" "$out/packages.json"
for arch in x86_64 aarch64; do
  pub=$d/published/master/latest/$arch
  for name in gvisor.tar.bz2 gvisor.tar.zstd; do
    (cd "$pub" && sha512sum --check --status "$name.sha512")
    cmp "$staged/$arch/$name" "$pub/$name"
  done
  cpu=amd64
  if [[ $arch == aarch64 ]]; then cpu=arm64; fi
  deb=$d/published/pool/20261029.0/binary-$cpu/runsc.deb
  [[ $(dpkg-deb -f "$deb" Architecture) == "$cpu" && $(dpkg-deb -f "$deb" Version) == 20261029.0 ]]
  cmp <(dpkg-deb --fsys-tarfile "$staged/$arch/runsc.deb") <(dpkg-deb --fsys-tarfile "$deb")
  # Use the exact staged binaries; check the ARM driver through user QEMU.
  member=$(tar -tjf "$staged/$arch/gvisor.tar.bz2" | awk '$0=="./runsc"||$0=="runsc"{print}')
  [[ $member == "./runsc" || $member == "runsc" ]]
  tar -xjf "$staged/$arch/gvisor.tar.bz2" -O "$member" > "$d/runsc-$arch"
  chmod 700 "$d/runsc-$arch"
  runner=()
  if [[ $arch == aarch64 ]]; then runner=(qemu-aarch64); fi
  "${runner[@]}" "$d/runsc-$arch" --version > "$out/version-$arch.txt"
  grep -Fx "runsc version $tag" "$out/version-$arch.txt"
done
gpg --batch --verify "$d/published/dists/master/Release.gpg" "$d/published/dists/master/Release" > "$d/signature.log" 2>&1
printf '%s\n' 'published tar SHA-512 and bytes, APT pool data payloads and release signature verified' > "$out/publication.txt"
