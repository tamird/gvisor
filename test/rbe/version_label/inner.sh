#!/bin/bash
set -euo pipefail
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
export VERSION_TEMP=$1
out=$VERSION_TEMP/result
fixture=test/rbe/version_label
baz="$PWD/$fixture/bazel.sh"
tag=release-20261029.0
mkdir -p "$VERSION_TEMP/native" "$out"
declare -A statuses
record() {
  local key=$1; shift
  set +e
  (set -euo pipefail; "$@") >"$VERSION_TEMP/$key.log" 2>&1
  local rc=$?
  set -e
  statuses[$key]=$rc
  printf '%s %s\n' "$key" "$rc" >>"$out/stages.txt"
}
make_opts=(DOCKER_BUILD=false "BAZEL=$baz" BAZEL_OPTIONS=--compilation_mode=opt)
control() {
  mkdir -p "$VERSION_TEMP/invalid"; local label
  for label in '' 0.0.0 VERSION_MISSING 'x;x1' 'tag-only'; do
    if make "${make_opts[@]}" RELEASE_ARTIFACTS="$VERSION_TEMP/invalid" release-artifacts RELEASE_VERSION="$label" > "$VERSION_TEMP/invalid-label" 2>&1; then return 1; fi
    grep -E 'invalid RELEASE_VERSION|RELEASE_VERSION must contain a digit' "$VERSION_TEMP/invalid-label"
    [[ ! -e $VERSION_TEMP/invalid/x86_64 && ! -e $VERSION_TEMP/invalid/aarch64 ]]
  done
  local dir=bazel-bin/test/rbe/version_label/probe_/probe
  "$baz" build --config=x86_64 -c opt --config=release_version --embed_label="$tag" //tools/bazeldefs:version //test/rbe/version_label:probe //tools/parsers:parser
  grep -Fx '20261029.0' bazel-bin/tools/bazeldefs/version.txt
  "$dir" "$tag" | grep -Fx "reported=$tag matches=true"
  "$dir" "$tag-other" | grep -Fx "reported=$tag matches=false"
  "$baz" build --config=x86_64 -c opt --workspace_status_command=test/rbe/version_label/legacy.sh --embed_label="$tag" //tools/bazeldefs:version //test/rbe/version_label:probe
  grep -Fx '20180102.3' bazel-bin/tools/bazeldefs/version.txt
  "$dir" legacy-release-20180102.3 | grep -Fx 'reported=legacy-release-20180102.3 matches=true'
  for label in '' 0.0.0; do
    "$baz" build --config=x86_64 -c opt --config=release_version --embed_label="$label" //test/rbe/version_label:probe
    if [[ -z $label ]]; then "$dir" VERSION_MISSING | grep -Fx 'reported=VERSION_MISSING matches=false'; else "$dir" "$label" | grep -Fx 'reported=0.0.0 matches=false'; fi
    if "$baz" build --config=x86_64 -c opt --config=release_version --embed_label="$label" //tools/bazeldefs:version >"$VERSION_TEMP/negative-label" 2>&1; then return 1; fi
    grep -F 'missing package version' "$VERSION_TEMP/negative-label"
  done
}
ordinary() {
  git describe --always --tags --exclude 'release-*-staging' --abbrev=12 --dirty > "$fixture/ordinary.label"
  local value
  value=$(cat "$fixture/ordinary.label")
  "$baz" build --config=x86_64 -c opt --remote_download_outputs=toplevel //debian:gvisor-release-tar-bz2 //tools/bazeldefs:version
  local v="${value#"${value%%[[:digit:]]*}"}"
  grep -Fx "$v" bazel-bin/tools/bazeldefs/version.txt
  cp -L bazel-bin/debian/gvisor.tar.bz2 "$fixture/ordinary.tar.bz2"
  printf '%s %s\n' "$value" "$v" > "$out/ordinary.txt"
}
release() {
  export GIT_CEILING_DIRECTORIES="$(dirname "$PWD")"
  # Exercise the normal two-architecture Make recipe with neither Git
  # metadata nor the legacy status script available to its Bazel calls.
  trap 'mv "$VERSION_TEMP/release-workspace-git" .git; mv "$VERSION_TEMP/release-workspace-status" tools/workspace_status.sh' EXIT
  mv .git "$VERSION_TEMP/release-workspace-git"
  mv tools/workspace_status.sh "$VERSION_TEMP/release-workspace-status"
  if git rev-parse --is-inside-work-tree > /dev/null 2>&1; then return 1; fi
  [[ ! -e tools/workspace_status.sh ]]
  mkdir -p "$VERSION_TEMP/release"/{x86_64,aarch64}
  make "${make_opts[@]}" RELEASE_ARTIFACTS="$VERSION_TEMP/release" RELEASE_VERSION="$tag" release-artifacts
  cp -L "$VERSION_TEMP/release/x86_64/gvisor.tar.bz2" "$fixture/release.tar.bz2"
  python3 "$fixture/packages.py" "$VERSION_TEMP/release" "$tag" "$out/packages.json"
}
restore() {
  [[ -s "$fixture/release.tar.bz2" && -s "$fixture/ordinary.tar.bz2" ]]
  "$baz" test --config=x86_64 -c opt --nocache_test_results --modify_execution_info='^TestRunner$=+no-remote-cache' --test_timeout=600 --remote_download_outputs=toplevel --remote_download_regex='.*[/]test[.](log|xml)$' //test/rbe/version_label:restore
  cp -L bazel-testlogs/test/rbe/version_label/restore/{test.log,test.xml} "$out/"
  grep -F 'VERSION-CHECK ' "$out/test.log"
}
record controls control
record ordinary ordinary
record release release
record restore restore
for result in "${statuses[@]}"; do [[ $result == 0 ]] || exit 1; done
