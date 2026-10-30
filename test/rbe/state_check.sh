set -euo pipefail
tools=$RUNNER_TEMP/state-reports
out=$RUNNER_TEMP/state-results
mkdir -p "$tools" "$out"
for script in qualification_report.py unit_matrix.py state_result.py; do
  git show "$GITHUB_SHA:test/rbe/$script" > "$tools/$script"
done
for arm in focused proposed; do
  src=62e214867e089f4ba2db0b79beecd8148c4da509
  git checkout --quiet --detach "$src"
  dir="$out/$arm"
  mkdir "$dir"
  git status --porcelain --untracked-files=no > "$dir/before"
  git cat-file commit HEAD > "$dir/commit"
  args=(test --config=x86_64 --jobs=4 --test_sharding_strategy=disabled --nocache_test_results --test_output=errors --build_event_publish_all_actions --nobuild_event_json_file_path_conversion --build_event_json_file="$RUNNER_TEMP/$arm.bep")
  labels=(//pkg/state/tests:tests_test)
  if [[ $arm == focused ]]; then
    args+=(--test_arg=-test.v '--test_filter=^Test(NestedValueConversion|BothEnginesStatefulCallbacks)$')
  fi
  printf '%s\n' "${labels[@]}" > "$dir/targets"
  printf '%s\n' "${args[@]}" "${labels[@]}" > "$dir/arguments"
  root=$(bazel --bazelrc="$rc" info output_base)
  native=0
  bazel --bazelrc="$rc" "${args[@]}" "${labels[@]}" > "$RUNNER_TEMP/$arm.log" 2>&1 || native=$?
  printf '%s\n' "$native" > "$dir/exit"
  date -u +%FT%T.%3NZ > "$dir/finish"
  git status --porcelain --untracked-files=no > "$dir/after"
  git rev-parse HEAD > "$dir/head"
  tail -14 "$RUNNER_TEMP/$arm.log"
  cap=0
  python3 -B "$tools/qualification_report.py" capture --events "$RUNNER_TEMP/$arm.bep" --output "$dir/events.jsonl" --output-root "$root" --report-dir "$dir/original" || cap=$?
  echo "$cap" > "$dir/capture-exit"
  python3 -B "$tools/state_result.py" context "$dir" "$src"
  verify=0
  python3 -B "$tools/qualification_report.py" verdict --events "$dir/events.jsonl" --context "$dir/context.json" --expected-targets "$dir/targets" --output "$dir/verdict.json" || verify=$?
  echo "$verify" > "$dir/verifier-exit"
done
python3 -B "$tools/state_result.py" final "$out"
