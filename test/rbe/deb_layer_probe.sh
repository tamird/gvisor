#!/usr/bin/env bash
set -euo pipefail
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
out="$RUNNER_TEMP/publication"
umask 077
auth="$RUNNER_TEMP/qualification-auth.bazelrc"
printf '%s\n' \
    'build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io' \
    'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io' \
    'build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io' \
    'build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/' > "$auth"
printf 'build:buildbuddy_remote_executor --remote_header=x-buildbuddy-api-key=%s\nbuild:buildbuddy_remote_executor --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY" "$BUILDBUDDY_API_KEY" >> "$auth"
unset BUILDBUDDY_API_KEY
baz=(bazelisk --bazelrc=test/rbe/qualification.bazelrc --bazelrc="$auth")
# The snapshot host can return 502 for these unchanged Noble packages.
# Bazel and the explicit pins both verify the same official mirror bytes.
dist="$RUNNER_TEMP/deb-inputs"
mkdir -p "$dist"
for arch in amd64 arm64; do
  case "$arch" in
    amd64) mirror=https://archive.ubuntu.com/ubuntu; hash=ce1a7283a3ffc47bc832dfd77c4fba6a327f3928f1c90c64fe14e251251cfc3a ;;
    arm64) mirror=https://ports.ubuntu.com/ubuntu-ports; hash=afc3f29f3c0ec4547c60985aa2373dc1dce3324cb3e8970f03b92c4789e74665 ;;
  esac
  name=erofs-utils_1.7.1-1build2_$arch.deb
  curl --fail --silent --show-error --location --retry 2 --max-time 45 "$mirror/pool/universe/e/erofs-utils/$name" -o "$dist/$name"
  printf '%s  %s\n' "$hash" "$dist/$name" | sha256sum --check --strict
done
flags=(--distdir="$dist" --config=rbe --config=x86_64 --noremote_accept_cached --remote_download_outputs=all --jobs=4)
targets=()
for arch in amd64 arm64; do
  targets+=("@@rules_distroless++_apt+++container_apt+noble_erofs-utils-${arch}_1.7.1-1build2//:data")
done
expect_missing_shell() {
  local result=0
  "${baz[@]}" build "${flags[@]}" --keep_going "${targets[@]}" > "$RUNNER_TEMP/stock.log" 2>&1 || result=$?
  # Executor errors may JSON-escape slashes in the command path.
  sed 's|\\/|/|g' "$RUNNER_TEMP/stock.log" > "$RUNNER_TEMP/stock.normalized"
  if [[ "$result" == 0 ]] || ! grep -m1 -Ei '/bin/bash.*(not found|no such file|does not exist|could not be found|cannot find)|(not found|no such file|does not exist|could not be found|cannot find).*/bin/bash' "$RUNNER_TEMP/stock.normalized" | cut -c1-480 > "$out/missing-shell.txt" || ! [[ -s "$out/missing-shell.txt" ]]; then
    tail -35 "$RUNNER_TEMP/stock.log" >&2
    return 1
  fi
}
capture() {
  local arm="$1"
  local resultroot="$RUNNER_TEMP/$arm"
  mkdir -p "$resultroot"
  "${baz[@]}" cquery "${flags[@]}" "set(${targets[*]})" --output=files > "$resultroot/paths"
  local path
  for arch in amd64 arm64; do
    path=$(grep "erofs-utils-$arch" "$resultroot/paths")
    test -f "$path" && [[ "$path" != *$'\n'* ]]
    cp "$path" "$resultroot/$arch.gz"
    sha256sum "$resultroot/$arch.gz" | cut -d' ' -f1 > "$out/$arm-$arch.sha256"
  done
}
expect_missing_shell
"${baz[@]}" build "${flags[@]}" --strategy=Genrule=local "${targets[@]}" > "$RUNNER_TEMP/control.log" 2>&1 || { tail -35 "$RUNNER_TEMP/control.log"; exit 1; }
capture control
cat >> MODULE.bazel <<'MODULE'

single_version_override(
    module_name = "rules_distroless",
    patch_strip = 1,
    patches = ["//tools:scratch-deb-layer.patch"],
)
MODULE
bazelisk mod graph --lockfile_mode=update --depth=1 --output=json > "$RUNNER_TEMP/fixed-graph.json"
"${baz[@]}" build "${flags[@]}" --execution_log_json_file="$RUNNER_TEMP/spawns.jsonl" "${targets[@]}" > "$RUNNER_TEMP/fixed.log" 2>&1 || { tail -35 "$RUNNER_TEMP/fixed.log"; exit 1; }
capture fixed
for arch in amd64 arm64; do cmp "$RUNNER_TEMP/control/$arch.gz" "$RUNNER_TEMP/fixed/$arch.gz"; done
python3 test/rbe/deb_layer_receipt.py
