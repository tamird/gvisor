set -euo pipefail
tools=$RUNNER_TEMP/rack-reports
out=$RUNNER_TEMP/rack-results
mkdir -p "$tools" "$out"
for script in qualification_report.py unit_matrix.py rack_ack_result.py; do
  git show "$GITHUB_SHA:test/rbe/$script" > "$tools/$script"
done
tcppkg=//pkg/tcpip/transport/tcp
rack=$tcppkg/test/e2e:tcp_rack_test
for arm in base current fixed; do
  case "$arm" in base) src=33da6267821e61e7d069fe1fbfb3fbc349181894;; current) src=5384c245648093b8a3b9a3b43504a00667e7e7d7;; fixed) src=c4cd1d33e44e1e1a6ccd66f937be3a0e27b8f0a0;; esac
  git checkout --quiet --detach "$src"
  dir="$out/$arm"
  mkdir "$dir"
  git status --porcelain --untracked-files=no > "$dir/before"
  git cat-file commit HEAD > "$dir/commit"
  args=(test --test_env=STATEFILE_NONROOT_PROBE=1 --config=x86_64 --jobs=4 --test_sharding_strategy=disabled --nocache_test_results --test_output=errors --build_event_publish_all_actions --nobuild_event_json_file_path_conversion --build_event_json_file="$RUNNER_TEMP/$arm.bep")
  labels=(//runsc/cmd:cmd_test)
  args+=(--test_arg=-test.v '--test_arg=-test.run=^TestStatefile(EdgeCases|OutputUnlistableDirectory)$')
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
  python3 -B "$tools/rack_ack_result.py" context "$dir" "$src"
  verify=0
  python3 -B "$tools/qualification_report.py" verdict --events "$dir/events.jsonl" --context "$dir/context.json" --expected-targets "$dir/targets" --output "$dir/verdict.json" || verify=$?
  echo "$verify" > "$dir/verifier-exit"
done
python3 -B "$tools/rack_ack_result.py" final "$out"
