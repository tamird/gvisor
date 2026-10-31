set -euo pipefail
out="$RUNNER_TEMP/rack-results"
tools="$RUNNER_TEMP/export-verifier"
mkdir -p "$tools" "$out"
for f in qualification_report.py unit_matrix.py export_report.py; do git show "$GITHUB_SHA:test/rbe/$f" > "$tools/$f"; done
for arm in stock direct; do
  src=b61651f9371f514411dbb4fc2bd367f860204546
  if [[ $arm == direct ]]; then src=bfc0d1dfb6edad51ac12b4d9bbdb55a1a59ec00d; fi
  git switch --quiet --detach "$src"
  d="$out/$arm"; mkdir "$d"
  git status --porcelain --untracked-files=no > "$d/before"
  git cat-file commit HEAD > "$d/commit"
  labels=(//:go_export)
  args=(build --config=x86_64 --jobs=12 --remote_download_outputs=toplevel --nobuild_event_json_file_path_conversion "--build_event_json_file=$RUNNER_TEMP/$arm.bep" "${labels[@]}")
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
  python3 -B "$tools/export_report.py" context "$d" "$src"
  verify=0
  python3 -B "$tools/qualification_report.py" verdict --events "$d/events.jsonl" --context "$d/context.json" --expected-targets "$d/targets" --output "$d/verdict.json" || verify=$?
  echo "$verify" >"$d/verifier-exit"
  if [[ $native == 0 ]]; then
    zip=bazel-bin/go_export.zip
    sha256sum "$zip" | cut -d' ' -f1 > "$d/sha256"
    unzip -Z1 "$zip" > "$d/entries"
    unzip -tq "$zip"
    bazel --bazelrc="$rc" aquery --config=x86_64 --output=jsonproto 'outputs("(^|/)go_export[.]zip$", //:go_export)' > "$RUNNER_TEMP/$arm.action"
    python3 -B "$tools/export_report.py" action "$d" "$RUNNER_TEMP/$arm.action"
  fi
done
python3 -B "$tools/export_report.py" final "$out"
