set -euo pipefail
name=$1
native=$2
case "$name" in
  ollama) product=1c5d0f089116d91c34262a4b63ebfc9fa7653e91; image=gpu/ollama/client; stem=client; package=//images/gpu/ollama/client; fixture=ollama_client_test; expected=TestOllamaClientImage ;;
  profile) product=6dd2bf31bde7cea6e0006856b634e4df64f5c3f2; image=benchmarks/profile-helper; stem=profile_helper; package=//images/benchmarks/profile-helper; fixture=profile_helper_test; expected=TestProfileHelperImage ;;
esac
case "$native" in x86_64) cpu=amd64 ;; aarch64) cpu=arm64 ;; esac
receipt="$RUNNER_TEMP/images-results/$name-$native"
mkdir -p "$receipt"
exec > >(tee "$receipt/coordinator.log") 2>&1
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
git checkout --quiet --detach "$product"
test "$(git rev-parse HEAD)" = "$product"
printf '%s\n' "$product" > "$receipt/product"

context_label="$package:${stem}_context"
case "$name/$native" in
  ollama/x86_64) producer=0722b900-dcce-4bcb-9847-cfcc2427c491 ;;
  ollama/aarch64) producer=b990aa86-61fb-4854-8b71-272cac5507e9 ;;
  profile/x86_64) producer=e00dc478-44ca-4b97-89e0-4b40481ebd16 ;;
  profile/aarch64) producer=7d25c1b9-8d0d-435b-b5da-79193b635940 ;;
esac
work=$(mktemp -d "$RUNNER_TEMP/consume-image-XXXX")
trap 'rm -rf "$work"' EXIT
git show "$GITHUB_SHA:test/rbe/recover-image-context.py" > "$work/recover.py"
if python3 "$work/recover.py" "$context_label" "$producer" "$receipt" "$work/context.tar"; then
  context_archive="$work/context.tar"
else
  # Reuse the original Bazel action cache. A remote cache miss fails instead of rebuilding this image.
  unset BUILDBUDDY_API_KEY
  bz build --experimental_remote_require_cached --config="$native" "$context_label"
  context_archive=$(bz cquery --config="$native" --output=files "$context_label")
  test -f "$context_archive"
  printf '%s\n' 'Original archive materialized with --experimental_remote_require_cached.' > "$receipt/materialization"
fi
unset BUILDBUDDY_API_KEY
tar -xOf "$context_archive" "./images/$image/image.tar" > "$work/image.tar" ||
  tar -xOf "$context_archive" "images/$image/image.tar" > "$work/image.tar"
docker load -i "$work/image.tar"
image_tag="gvisor.dev/images/$image:latest"
test "$(docker image inspect -f '{{.Architecture}}' "$image_tag")" = "$cpu"
git checkout --quiet --detach "$GITHUB_SHA"
bz test --strategy=TestRunner=local --nocache_test_results --test_output=errors --test_arg=-test.v --test_timeout=240 --run_under="$RUNNER_TEMP/privileged-fixture" "//test/rbe:$fixture"
log="bazel-testlogs/test/rbe/$fixture/test.log"
cp "$log" "$receipt/test.log"
cp "bazel-testlogs/test/rbe/$fixture/test.xml" "$receipt/test.xml"
grep -F -- "--- PASS: $expected " "$receipt/test.log"
if [[ "$name" == profile ]]; then grep -F -- '--- PASS: TestProfileHelperImage/containerd_flags ' "$receipt/test.log"; fi
if [[ "$name" == ollama ]]; then grep -F -- '--- PASS: TestOllamaClientImage/system_certificates ' "$receipt/test.log"; fi
! grep -E -- '--- (FAIL|SKIP): ' "$receipt/test.log"
echo passed > "$receipt/verdict"
