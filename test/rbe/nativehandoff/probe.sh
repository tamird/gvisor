#!/bin/bash
set -euo pipefail
r=$RELEASE_TEMP
out=$r/repository
tag=release-20261029.0
artifacts=bazel-bin/debian/release-artifacts
args=("RELEASE_KEY=$r/key" "RELEASE_ROOT=$out" "DOCKER_CLI_PATH=$r/no-docker/docker")
# Declare a private cache setting to catch a regression to copying out of Docker.
record() {
  local stage=$1 rc=0
  shift
  "$@" >"$r/$stage.log" 2>&1 || rc=$?
  printf '%s %s\n' "$stage" "$rc" >> "$RESULT/exits"
  if ((rc)); then
    # Do not publish client configuration, command lines or credentials.
    sed -nE '/^(ERROR:|make: \*\*\*|Missing release artifact:)/p' "$r/$stage.log" | tail -10 | sed -E 's/(x-buildbuddy-api-key=)[^ ]+/\1REDACTED/g' >&2
  fi
  return "$rc"
}
record build-sign make "BAZEL=bazelisk --bazelrc=$r/private.bazelrc" "BAZEL_OPTIONS=--compilation_mode=opt --build_event_json_file=$r/native.bep" "RELEASE_VERSION=$tag" BAZEL_CACHE_USE_VOLUME=true "${args[@]}" release
test ! -e artifacts/native
test ! -e "$r/docker-attempt"
for arch in x86_64 aarch64; do
  d="$out/master/latest/$arch"
  for leaf in gvisor.tar.bz2 gvisor.tar.zstd; do
    (cd "$d" && sha512sum --check --status "$leaf.sha512")
    cmp "$artifacts/$arch/$leaf" "$d/$leaf"
  done
  cpu=amd64; [[ "$arch" == x86_64 ]] || cpu=arm64
  deb="$out/pool/20261029.0/binary-$cpu/runsc.deb"
  test "$(dpkg-deb -f "$deb" Architecture)" = "$cpu"
  test "$(dpkg-deb -f "$deb" Version)" = 20261029.0
  dpkg-sig --verify "$deb" | grep -q GOODSIG
done
gpg --batch --verify "$out/dists/master/Release.gpg" "$out/dists/master/Release"
echo verified > "$RESULT/signatures"
# Supply five real inputs and omit the final tar. No Bazel build or signer
# should run; the missing file must be named before the output tree is populated.
mkdir -p "$r/prebuilt"/{x86_64,aarch64}
for arch in x86_64 aarch64; do
  for leaf in runsc.deb gvisor.tar.bz2 gvisor.tar.zstd; do
    [[ "$arch/$leaf" == aarch64/gvisor.tar.zstd ]] && continue
    ln -s "$PWD/$artifacts/$arch/$leaf" "$r/prebuilt/$arch/$leaf"
  done
done
if record missing make BAZEL=/nonexistent "RELEASE_KEY=$r/key" "RELEASE_ARTIFACTS=$r/prebuilt" "RELEASE_ROOT=$r/incomplete" release; then
  exit 1
fi
grep -F "Missing release artifact: $r/prebuilt/aarch64/gvisor.tar.zstd" "$r/missing.log" >/dev/null
test -z "$(find "$r/incomplete" -mindepth 1 -print -quit)"
test ! -e "$r/docker-attempt"
echo aarch64/gvisor.tar.zstd > "$RESULT/rejected"
