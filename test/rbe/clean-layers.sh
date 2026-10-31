set -euo pipefail
arch=$1
r="$RUNNER_TEMP/oci-clean-$arch";mkdir -p "$r"
if [[ $# == 1 ]]; then
  failures=0
  for image in profile ollama hey hackbench; do
    result=0; bash "$0" "$arch" "$image" || result=$?
    printf '%s %s\n' "$image" "$result" | tee -a "$r/status"
    if [[ "$result" != 0 ]]; then failures=1; fi
  done
  [[ "$failures" == 0 ]]
  echo PASS >"$r/verdict"
  exit
fi
image=$2
exec > >(tee "$r/$image.log") 2>&1
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
key=$image-$arch
 source=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]]["corrected"])' "$RUNNER_TEMP/clean-layer-expectations.json" "$key")
 git checkout -q --detach "$source"
 git rev-parse HEAD >"$r/$image-head";grep -xF "$source" "$r/$image-head"
 case "$image" in
   profile) pkg=//images/benchmarks/profile-helper;stem=profile_helper;;
   ollama) pkg=//images/gpu/ollama/client;stem=client;;
   hey|hackbench) pkg=//images/benchmarks/$image;stem=$image;;
 esac
 bz build --config="$arch" "$pkg:${stem}_image" "$pkg:${stem}_archive"
 bz cquery --config="$arch" --output=files "$pkg:${stem}_image" >"$r/$image-manifest-files"
 bz cquery --config="$arch" --output=files "$pkg:${stem}_archive" >"$r/$image-archive-files"
 python3 "$RUNNER_TEMP/verify-image-runtime-files.py" "$RUNNER_TEMP/clean-layer-expectations.json" "$key" "$r/$image-manifest-files" "$r/$image-archive-files" "$r/$image.json"
