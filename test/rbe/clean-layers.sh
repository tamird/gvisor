set -euo pipefail
arch=$1
r="$RUNNER_TEMP/oci-clean-$arch"
mkdir -p "$r"
if [[ $# == 1 ]]; then
  failures=0
  for image in profile ollama hey; do
    rc=0; bash "$0" "$arch" "$image" || rc=$?
    printf '%s %s\n' "$image" "$rc" | tee -a "$r/status"
    (( rc == 0 )) || failures=1
  done
  (( failures == 0 ))
  echo PASS >"$r/verdict"
  exit
fi
image=$2
exec > >(tee "$r/$image.log") 2>&1
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
case "$image" in
  profile) rel=benchmarks/profile-helper; stem=profile_helper; testlabel=//test/rbe:profile_helper_test; names=(TestProfileHelperImage TestProfileHelperImage/containerd_flags);;
  ollama) rel=gpu/ollama/client; stem=client; testlabel=//test/rbe:ollama_client_test; names=(TestOllamaClientImage TestOllamaClientImage/system_certificates);;
  hey) rel=benchmarks/hey; stem=hey; testlabel=//test/benchmarks/tools:hey_image_test; names=(TestHeyImage TestHeyImage/http TestHeyImage/https);;
  *) exit 2;;
esac
pkg=//images/$rel
key=$image-$arch
expected="$RUNNER_TEMP/clean-layer-expectations.json"
old=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]]["source"])' "$expected" "$key")
args=("--config=$arch" "$pkg:${stem}_image" "$pkg:${stem}_archive")
before="$RUNNER_TEMP/$key-before"
mkdir -p "$before"
if [[ ! -s "$before/digests.json" ]]; then
  git checkout -q --detach "$old"
  test "$(git rev-parse HEAD)" = "$old"
  # The original client already built these outputs; forbid re-execution.
  if bz build --experimental_remote_require_cached "${args[@]}" &&
     bz cquery --config="$arch" --output=files "set($pkg:${stem}_image $pkg:${stem}_archive)" >"$before/paths"; then
    python3 "$RUNNER_TEMP/verify-image-runtime-files.py" digests "$before/paths" "$before/digests.json"
  fi
fi
git checkout -q --detach "$SOURCE"
test "$(git rev-parse HEAD)" = "$SOURCE"
test -z "$(git status --porcelain --untracked-files=no)"
bz build "${args[@]}" "$pkg:${stem}_context"
bz cquery --config="$arch" --output=files "set($pkg:${stem}_image $pkg:${stem}_archive $pkg:${stem}_context)" >"$r/$image-paths"
python3 "$RUNNER_TEMP/verify-image-runtime-files.py" inspect "$expected" "$key" "$r/$image-paths" "$before/digests.json" "$r/$image.json" "$rel" "$SOURCE"
make rebuild-"${rel//\//_}" DOCKER_BUILD=false ARCH="$arch" STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
tag=gvisor.dev/images/$rel:latest
docker image inspect "$tag" --format '{{.Id}} {{.Architecture}}' >"$r/$image-loaded"
python3 "$RUNNER_TEMP/verify-image-runtime-files.py" loaded "$r/$image.json" "$r/$image-loaded"
git checkout -q --detach "$GITHUB_SHA"
rc=0
bz test --strategy=TestRunner=local --nocache_test_results --test_output=errors --test_arg=-test.v --test_timeout=240 --run_under="$RUNNER_TEMP/privileged-fixture" "$testlabel" || rc=$?
d="bazel-testlogs/${testlabel#//}"; d="${d/:/\/}"
if [[ -f "$d/test.xml" ]]; then cp "$d/test.xml" "$r/$image-test.xml"; fi
if [[ -f "$d/test.log" ]]; then cp "$d/test.log" "$r/$image-test.log"; fi
(( rc == 0 ))
test -s "$r/$image-test.xml"
for name in "${names[@]}"; do grep -F -- "--- PASS: $name " "$r/$image-test.log"; done
if grep -Eq -- '--- (SKIP|FAIL): ' "$r/$image-test.log"; then exit 1; fi
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["same_original_outputs"]' "$r/$image.json"
