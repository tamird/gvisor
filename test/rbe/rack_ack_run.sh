set -euo pipefail
out="$RUNNER_TEMP/rack-results"
mkdir -p "$out"
git show "$GITHUB_SHA:test/rbe/status_receipt.py" >"$RUNNER_TEMP/status_receipt.py"
stock=49c59fcc0063fa0e98d2b383947e4c238be0f21a
product=8529bfdd3776d087b9d705df0c5232a8337113ab
for src in "$stock" "$product"; do
  git fetch --no-tags --depth=2 origin "$src"
  git checkout --quiet --detach "$src"
  original=$(git rev-parse 'HEAD^{tree}')
  python3 - <<'PY'
from pathlib import Path
p=Path('tools/bazeldefs/BUILD')
s=p.read_text()
key='    name = "version",\n'
assert s.count(key)==1
line='    exec_properties = {"container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"},\n'
p.write_text(s.replace(key,key+line))
PY
  git add -- tools/bazeldefs/BUILD
  git -c user.name=Qualifier -c user.email=qualifier@example.com -c commit.gpgSign=false commit --quiet -m 'Direct only the package version action to the empty Linux worker'
  lanes=(stock)
  if [[ "$src" == "$product" ]]; then
    git -c user.name=Qualifier -c user.email=qualifier@example.com tag -a dev-20981231.4 -m 'ordinary release fixture' HEAD
    lanes=(staged-amd64 nightly-amd64 staged-arm64 retag-amd64)
  fi
  for lane in "${lanes[@]}"; do
    arch=amd64; mode=${lane%-*}; tag=release-20991231.7
    if [[ "$lane" == *arm64 ]]; then arch=arm64; fi
    if [[ "$mode" == nightly ]]; then tag=dev-20981231.4; fi
    if [[ "$mode" == retag ]]; then tag=release-20991231.8; fi
    d="$out/$lane"; mkdir -p "$d"
    echo "$src" > "$d/source"
    git rev-parse HEAD > "$d/control"
    git diff "$original" HEAD > "$d/worker-only.patch"
    args=(--jobs=8 --remote_download_outputs=toplevel --noshow_progress --nobuild_event_json_file_path_conversion
      "--platforms=//tools/bazeldefs:linux_${arch}" '--modify_execution_info=^(PackageVersion|Genrule)$=+no-remote-cache'
      --strategy=GoStdlib=linux-sandbox --strategy=GoStdlibList=linux-sandbox
      --strategy=GoStandardLibraryAnalysis=linux-sandbox --strategy=GoStaticAnalysis=linux-sandbox
      --noremote_upload_local_results)
    if [[ "$mode" != nightly ]]; then args+=(--config=staged_release "--embed_label=$tag"); fi
    set +e
    bazel --bazelrc="$rc" build "${args[@]}" "--execution_log_json_file=$RUNNER_TEMP/$lane.exec" \
      "--build_event_json_file=$RUNNER_TEMP/$lane.bep" //tools/bazeldefs:version > "$RUNNER_TEMP/$lane.log" 2>&1
    result=$?
    set -e
    tail -45 "$RUNNER_TEMP/$lane.log" > "$d/build.log"; cat "$d/build.log"
    root=$(bazel --bazelrc="$rc" info execution_root)
    if ! python3 "$RUNNER_TEMP/status_receipt.py" "$d" "$RUNNER_TEMP/$lane.exec" "$RUNNER_TEMP/$lane.bep" "$root" "$result" "$mode" "$arch" "$tag"; then
      echo "Verification failed for $lane"; echo failed > "$d/verdict"
    fi
  done
done
python3 - "$out" <<'PY'
import json,sys
from pathlib import Path
d=Path(sys.argv[1])
names=['stock','staged-amd64','nightly-amd64','staged-arm64','retag-amd64']
for n in names: assert (d/n/'verdict').read_text()=='verified\n',n
rows=[json.loads((d/n/'action.json').read_text()) for n in names[1:]]
assert rows[0]['package_sha256']==rows[2]['package_sha256']
assert rows[0]['action_digest'] !=rows[3]['action_digest']
(d/'verdict').write_text('All five original worker receipts verified.\n')
PY
