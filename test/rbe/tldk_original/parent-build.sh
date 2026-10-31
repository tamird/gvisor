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
# The public image does not define the GitHub runner UID. Supply it via
# the ordinary passwd database; keep all built outputs owned by the runner.
docker run --rm --entrypoint=cat "$builder" /etc/passwd > "$d/builder.passwd"
if ! awk -F: -v uid="$(id -u)" '$3==uid{found=1} END{exit !found}' "$d/builder.passwd"; then
  printf 'runner:x:%s:%s:Hosted runner:%s:/bin/bash\n' "$(id -u)" "$(id -g)" "$d/tldk-builder-home" >> "$d/builder.passwd"
fi
docker run --rm --entrypoint=cat "$builder" /etc/group > "$d/builder.group"
if ! awk -F: -v gid="$(id -g)" '$3==gid{found=1} END{exit !found}' "$d/builder.group"; then
  printf 'runner:x:%s:\n' "$(id -g)" >> "$d/builder.group"
fi
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
  -v "$d/builder.passwd:/etc/passwd:ro" -v "$d/builder.group:/etc/group:ro" -e USER=runner -e LOGNAME=runner -e "HOME=$d/tldk-builder-home" -e RUNNER_TEMP -e "GIT_CONFIG_COUNT=1" -e "GIT_CONFIG_KEY_0=safe.directory" -e "GIT_CONFIG_VALUE_0=$PWD" "$builder" -c '
  set -euo pipefail
  d=$RUNNER_TEMP
  test -n "$(getent passwd "$(id -u)")"
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
