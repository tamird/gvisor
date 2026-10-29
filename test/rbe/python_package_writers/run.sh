#!/bin/bash
# Invoke the existing remote build lane and retain its unmodified native result.
set -euo pipefail
here=test/rbe/python_package_writers
rbe_key=$BUILDBUDDY_API_KEY
unset BUILDBUDDY_API_KEY
out=$RUNNER_TEMP/python-packaging
mkdir -p "$out"
rc=$out/analysis.bazelrc
printf '%s\n' 'build:buildbuddy_remote_executor --define=writer_graph=true' > "$rc"
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
git show "$BASE:MODULE.bazel.lock" > MODULE.bazel.lock
bazelisk --bazelrc="$rc" mod graph --lockfile_mode=update --depth=1 --output=json > "$out/graph.json"
python3 - "$out/graph.json" <<'PY'
import json,sys
g=json.load(open(sys.argv[1]));d=[d for d in g["dependencies"] if d["key"].split("@")[0]=="rules_python"]
assert len(d)==1 and d[0]["key"]=="rules_python@2.4.5",d
PY
arch=$QUALIFICATION_ARCH
extra=()
if [[ $MODE == normal ]]; then
  extra+=(--host_platform=//"$here":ubuntu_"$arch" --extra_execution_platforms=//"$here":ubuntu_"$arch")
fi
export QUALIFICATION_TARGETS="//${here}:payload //${here}:package"
args=()
for opt in "${extra[@]}"; do args+=("--bazel-option=$opt"); done
set +e
BUILDBUDDY_API_KEY=$rbe_key bash test/rbe/actions.sh "${args[@]}" --bazel-option=--remote_download_outputs=all
native=$?
set -e
printf '%s\n' "$native" > "$out/native-exit.txt"
python3 "$here/contents.py" "$out"
exit "$native"
