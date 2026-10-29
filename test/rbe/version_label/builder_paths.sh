#!/bin/bash
# Fork-only qualification. This is the publisher, not the Bazel container.
set -euo pipefail
d=$1
out=$d/result
mkdir -m700 "$d/keyring"
export GNUPGHOME=$d/keyring
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
tag=release-20261029.0
[[ -z $(git tag --points-at HEAD) ]]
[[ ! -x /opt/builder-bazel && ! -e /root/.cache/bazel/_bazel_root ]]
for tool in bazel bazelisk; do
  toolpath=$(type -P "$tool") || continue
  chmod a-x "$toolpath"
done
! command -v bazel >/dev/null
! command -v bazelisk >/dev/null
args=(DOCKER_NAME=release-builder BAZEL=/opt/builder-bazel BAZEL_CACHE_USE_VOLUME=true BAZEL_OPTIONS=--compilation_mode=opt)
record() {
  local op=$1; shift
  local rc=0
  "$@" >"$d/$op.log" 2>&1 || rc=$?
  printf '%s %s\n' "$op" "$rc" >> "$out/stage-exits.txt"
  return "$rc"
}
finish() {
  local rc=$? report=0
  python3 test/rbe/version_label/path_verdict.py "$d" "$rc" || report=$?
  chmod -R a+rX "$out"
  ((rc)) && exit "$rc"
  exit "$report"
}
trap finish EXIT
# Preview actual Make goals and verify each selects the existing Docker server.
# Do not pass DOCKER_BUILD; the job's Make invocation must select it.
make -n "${args[@]}" release RELEASE_ROOT="$d/unstamped" RELEASE_KEY="$d/private.key" > "$d/unstamped-plan"
grep -E 'docker exec .*release-builder /opt/builder-bazel build .*//debian:release-artifacts' "$d/unstamped-plan" > "$out/unstamped-command"
! grep -q 'embed_label=' "$out/unstamped-command"
make -n "${args[@]}" "RELEASE_VERSION=$tag" release RELEASE_ROOT="$d/stamped" RELEASE_KEY="$d/private.key" > "$d/stamped-plan"
grep -E 'docker exec .*release-builder /opt/builder-bazel build .*--embed_label=.*//debian:release-artifacts' "$d/stamped-plan" > "$out/stamped-command"
record stamped make "${args[@]}" "RELEASE_VERSION=$tag" release RELEASE_ROOT="$d/stamped" RELEASE_KEY="$d/private.key"
staged="$PWD/artifacts/native"
[[ ! -e bazel-bin/debian/release-artifacts ]]
for arch in x86_64 aarch64; do
  for ext in bz2 zstd; do
    f="gvisor.tar.$ext"
    cd "$d/stamped/master/latest/$arch"
    sha512sum --check --status "$f.sha512"
    cmp "$staged/$arch/$f" "$f"
    cd - >/dev/null
  done
  cpu=amd64
  [[ "$arch" == x86_64 ]] || cpu=arm64
  deb="$d/stamped/pool/20261029.0/binary-$cpu/runsc.deb"
  [[ $(dpkg-deb -f "$deb" Version) == 20261029.0 && $(dpkg-deb -f "$deb" Architecture) == "$cpu" ]]
  dpkg-sig --verify "$deb" > "$d/deb-$cpu-signature"
  grep -q GOODSIG "$d/deb-$cpu-signature"
done
gpg --batch --verify "$d/stamped/dists/master/Release.gpg" "$d/stamped/dists/master/Release" >"$d/signature.log" 2>&1
# With both Docker and the host Bazel unavailable, signing a known directory
# exercises the contract used by the GitHub release mirror.
docker stop release-builder > /dev/null
record prebuilt env DOCKER_HOST="unix://$d/no-socket" make DOCKER_CLI_PATH=/does/not/exist BAZEL=/does/not/exist "RELEASE_ARTIFACTS=$staged" "RELEASE_ROOT=$d/prebuilt" "RELEASE_KEY=$d/private.key" release
gpg --batch --verify "$d/prebuilt/dists/master/Release.gpg" "$d/prebuilt/dists/master/Release" >"$d/prebuilt-signature.log" 2>&1
printf 'stamped-debs amd64 arm64\nrelease-signatures stamped prebuilt\n' > "$out/signatures"
