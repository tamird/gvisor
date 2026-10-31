set -euo pipefail
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
builder=gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a
d=$RUNNER_TEMP
docker pull "$builder" >/dev/null
# The unmodified parent genrule is local and uses git, GNU make, and libnuma-dev.
# Run it in the public builder itself, using its actual source-version Bazel.
docker run --rm --entrypoint=/bin/bash "$builder" -c 'set -e; bazel --version; gcc -dumpversion; dpkg-query -W libnuma-dev' > "$d/receipt/parent-builder.txt"
grep -Fx "bazel $USE_BAZEL_VERSION" "$d/receipt/parent-builder.txt"
grep -Fx 11 "$d/receipt/parent-builder.txt"
mkdir -p "$d/tldk-builder-home"
chmod 700 "$d/tldk-builder-home"
cp "$d/images.bazelrc" "$d/parent.bazelrc"
cat >> "$d/parent.bazelrc" <<'RC'
build --strategy=Genrule=local
build --strategy=CppCompile=local
build --strategy=GoStdlib=remote,local
build --strategy=GoStdlibList=remote,local
build --jobs=5
build --remote_download_outputs=toplevel
RC
docker run --rm --user "$(id -u):$(id -g)" --entrypoint=/bin/bash \
  -v "$PWD:$PWD" -v "$RUNNER_TEMP:$RUNNER_TEMP" -w "$PWD" \
  -e "HOME=$d/tldk-builder-home" -e RUNNER_TEMP -e "GIT_CONFIG_COUNT=1" -e "GIT_CONFIG_KEY_0=safe.directory" -e "GIT_CONFIG_VALUE_0=$PWD" "$builder" -c '
  set -euo pipefail
  d=$RUNNER_TEMP
  b() { bazel "--bazelrc=$d/parent.bazelrc" "$@"; }
  targets=(//runsc:runsc-plugin-stack //runsc/cmd/sentry:gvisor_sentry_plugin_stack)
  b build --config=x86_64 --config=plugin-tldk --build_event_json_file="$d/parent.bep" "${targets[@]}" > "$d/parent.log" 2>&1
  for label in "${targets[@]}"; do
    src=$(b cquery --config=x86_64 --config=plugin-tldk --output=files "$label" 2>"$d/cquery.log")
    dest=runsc; if test "$label" = //runsc/cmd/sentry:gvisor_sentry_plugin_stack; then dest=gvisor-bin/gvisor_sentry_plugin_stack; fi
    test -f "$src"
    install -m755 "$src" "$d/reused/$dest"
  done
  b shutdown'
