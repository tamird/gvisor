set -euo pipefail
out="$RUNNER_TEMP/rack-results"
tools="$RUNNER_TEMP/version-verifier"
mkdir -p "$tools" "$out"
for f in qualification_report.py unit_matrix.py label_result.py; do git show "$GITHUB_SHA:test/rbe/$f" > "$tools/$f"; done
label=release-20991231.7
git tag "$label" 7317e2f45ed1410ff688ebaaf9c0e4ff1c7ce6e9
for arm in stock named-amd64 named-arm64 no-label; do
  src=4bc4817ed9de82d61597531c9510a55967ace70d
  arch=x86_64
  expected=$label
  case $arm in
    stock) src=7317e2f45ed1410ff688ebaaf9c0e4ff1c7ce6e9;;
    named-arm64) arch=aarch64;;
    no-label) expected='';;
  esac
  git switch --quiet --detach "$src"
  if [[ $arm != stock ]]; then test ! -e tools/workspace_status.sh; fi
  d="$out/$arm"; mkdir "$d"
  git status --porcelain --untracked-files=no > "$d/before"
  git cat-file commit HEAD > "$d/commit"
  labels=(//runsc:runsc //tools/bazeldefs:version)
  args=(build "--config=$arch" "--embed_label=$expected" --jobs=12 --remote_download_outputs=toplevel --nobuild_event_json_file_path_conversion "--build_event_json_file=$RUNNER_TEMP/$arm.bep" "${labels[@]}")
  printf '%s\n' "${labels[@]}" >"$d/targets"
  printf '%s\n' "${args[@]}" >"$d/arguments"
  native=0
  output_base=$(bazel --bazelrc="$rc" info output_base)
  bazel --bazelrc="$rc" "${args[@]}" >"$RUNNER_TEMP/$arm.log" 2>&1 || native=$?
  date -u +%FT%T.%3NZ >"$d/finish"
  printf '%s\n' "$native" >"$d/exit"
  tail -16 "$RUNNER_TEMP/$arm.log"
  git status --porcelain --untracked-files=no >"$d/after"
  git rev-parse HEAD >"$d/head"
  capture=0
  python3 -B "$tools/qualification_report.py" capture --events "$RUNNER_TEMP/$arm.bep" --output "$d/events.jsonl" --output-root "$output_base" --report-dir "$d/original" || capture=$?
  echo "$capture" >"$d/capture-exit"
  python3 -B "$tools/label_result.py" context "$d" "$src"
  verify=0
  python3 -B "$tools/qualification_report.py" verdict --events "$d/events.jsonl" --context "$d/context.json" --expected-targets "$d/targets" --output "$d/verdict.json" || verify=$?
  echo "$verify" >"$d/verifier-exit"
  if [[ $native == 0 ]]; then
    file bazel-bin/runsc/runsc_/runsc >"$d/file"
    cp bazel-bin/tools/bazeldefs/version.txt "$d/version.txt"
    if [[ $arch == aarch64 ]]; then runner=(qemu-aarch64); else runner=(); fi
    smoke=0
    "${runner[@]}" bazel-bin/runsc/runsc_/runsc --version >"$d/cli" 2>"$d/cli-stderr" || smoke=$?
    echo "$smoke" >"$d/smoke"
  fi
done
python3 -B "$tools/label_result.py" final "$out"
