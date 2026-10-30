#!/bin/bash
set -euo pipefail
PRODUCT=$1
stage_name=$2
test_case=$3
set -euo pipefail
out="$RUNNER_TEMP/records/$stage_name"
mkdir -p "$out"
git fetch --no-tags --depth=2 origin "$PRODUCT"
carrier="$GITHUB_SHA"
trap 'git checkout --quiet --detach "$carrier"' EXIT
git checkout --quiet --detach "$PRODUCT"
git rev-parse HEAD > "$out/product"
docker exec -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0="$GITHUB_WORKSPACE" overlay-guest bazel --bazelrc="$RUNNER_TEMP/overlay.bazelrc" build --config=x86_64 -c opt --jobs=25 --remote_download_outputs=toplevel //:release //test/runner:runner //test/runner/setup_container //test/syscalls/linux:vfio_pci_device_test //test/syscalls:vfio_pci_device_test_native //test/syscalls:vfio_pci_device_test_runsc_systrap > "$RUNNER_TEMP/product-build.log" 2>&1 || { grep -A15 -E 'ERROR:|INFO: Invocation ID:' "$RUNNER_TEMP/product-build.log" | tail -80 | tee "$out/build-receipt"; exit 1; }
grep -E 'INFO: Invocation ID:|Build completed' "$RUNNER_TEMP/product-build.log" > "$out/build-receipt"
docker exec -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0="$GITHUB_WORKSPACE" overlay-guest bazel --bazelrc="$RUNNER_TEMP/overlay.bazelrc" query --output=build 'set(//test/syscalls:vfio_pci_device_test_native //test/syscalls:vfio_pci_device_test_runsc_systrap)' > "$out/target-arguments"
test "$(grep -Fc -- '--add-host-vfio' "$out/target-arguments")" = 2
test "$(grep -Fc -- 'exclusive-if-local' "$out/target-arguments")" = 2
stage="$RUNNER_TEMP/suite-$stage_name"
mkdir -p "$stage/release"
for platform in native runsc_systrap; do
  docker exec overlay-guest cp -L "bazel-bin/test/syscalls/vfio_pci_device_test_$platform" "$stage/$platform"
  cp "$stage/$platform" "$out/$platform-wrapper"
done
runner=$(awk '$1=="exec"{print $2}' "$stage/native")
test_bin=$(awk '$1=="exec"{print $NF}' "$stage/native")
[[ "$runner" == test/runner/* && "$test_bin" == test/syscalls/linux/* ]]
setup=test/runner/setup_container/setup_container
for path in "$runner" "$test_bin" "$setup"; do
  mkdir -p "$stage/$(dirname "$path")"
  docker exec overlay-guest cp -L "bazel-bin/$path" "$stage/$path"
  test -x "$stage/$path"
done
docker exec overlay-guest cp -RL --preserve=mode bazel-bin/release/. "$stage/release"
for name in runsc gvisor-bin/gvisor_sentry gvisor-bin/checkpointgofer gvisor-bin/gvisor-sentry-prewarmer gvisor-bin/gvisor-proc-umounter gvisor-bin/runsc-fd-parking gvisor-bin/runsc-metric-server; do test -x "$stage/release/$name"; done
sha256sum "$stage/$runner" "$stage/$test_bin" "$stage/$setup" "$stage"/{native,runsc_systrap,release/runsc,release/gvisor-bin/*} | sed "s#$stage/##" > "$stage/sha256"
cp "$stage/sha256" "$out/product-hashes"
for path in "$runner" "$test_bin" "$setup"; do
  docker exec overlay-guest ldd "$stage/$path" >> "$out/ldd" 2>&1 || { file "$stage/$path" | grep -Eq 'statically linked|static-pie linked'; }
done
if grep -q 'not found' "$out/ldd"; then cat "$out/ldd"; exit 1; fi
awk '$2=="=>" && $3~/^\//{print substr($3,2)} $1~/^\//{print substr($1,2)}' "$out/ldd" | sort -u > "$RUNNER_TEMP/libs"
grep -q . "$RUNNER_TEMP/libs"
if grep -vE '^(lib|usr/lib)' "$RUNNER_TEMP/libs"; then exit 1; fi
docker exec overlay-guest tar -chf "$stage/runtime-libs.tar" -C / -T "$RUNNER_TEMP/libs"
echo "$test_case" > "$stage/case-name"
tar -cf "$RUNNER_TEMP/vfio_suite.tar" -C "$stage" .
git checkout --detach "$carrier"
cp "$RUNNER_TEMP/vfio_suite.tar" test/rbe/tcg/
set -euo pipefail
target=//test/rbe/tcg:vfio_permanent_test
rc=0
docker exec -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0="$GITHUB_WORKSPACE" overlay-guest bazel --bazelrc="$RUNNER_TEMP/overlay.bazelrc" test --config=x86_64 -c opt --jobs=25 --remote_download_outputs=toplevel --strategy=TestRunner=local --nozip_undeclared_test_outputs --nocache_test_results --test_output=all --test_timeout=500 "$target" > "$RUNNER_TEMP/permanent.log" 2>&1 || rc=$?
echo "$rc" >"$out/guest-exit"
grep -E 'INFO: Invocation ID:|ERROR:|FAILED|failed|VfioPCI|VFIO|Build completed' "$RUNNER_TEMP/permanent.log" | tail -75 >"$out/guest-receipt" || :
for path in test.log test.outputs/console.log test.outputs/guest/vfio-device.txt test.outputs/guest/outputs/identity.txt test.outputs/guest/outputs/native.txt test.outputs/guest/outputs/runsc_systrap.txt test.outputs/guest/outputs/runsc_systrap_direct.txt test.outputs/guest/outputs/verdict.txt test.outputs/guest/outputs/runtime-diagnostics.txt; do
  name=${path##*/}
  docker exec overlay-guest cat "bazel-testlogs/test/rbe/tcg/vfio_permanent_test/$path" | tail -160 > "$out/$name" || :
done
test "$rc" = 0
for mode in native runsc_systrap runsc_systrap_direct; do grep -q "OK .*VfioPCIDeviceTest.$test_case" "$out/$mode.txt"; grep -Fq "$mode: 0" "$out/verdict.txt"; done
