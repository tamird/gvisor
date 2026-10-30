set -euo pipefail
tools="$RUNNER_TEMP/graph-tools"; out="$RUNNER_TEMP/graph-results"
mkdir -p "$tools" "$out"
for f in qualification_report.py unit_matrix.py state_result.py graph_pair_verdict.py; do git show "$GITHUB_SHA:test/rbe/$f" > "$tools/$f"; done
filter='^Test(IndexSparseAliasing|IndexWideNeighborhoods|SharedSliceWindows|OverlappingArrayPointersAndSlicesGraph|SliceOfArraysInteriorPointer|AddrMap)$'
labels=(//pkg/state:state_test //pkg/state/tests:tests_test)
common=(--config=x86_64 -c opt --jobs=12 --remote_download_outputs=toplevel)
baz=(bazel --bazelrc="$rc")
prune(){ grep -E '^(ERROR:|FAIL:|INFO: Streaming|[^ ]+\.go:[0-9]+:)' "$1" | head -20 || true; }
prepare() (
 set -eu
 arm=$1 src=$2 dir="$out/$1"
 mkdir -p "$dir"; git checkout --quiet --detach "$src"
 test "$(git rev-parse HEAD)" = "$src"
 git status --porcelain --untracked-files=no >"$dir/before"
 git cat-file commit HEAD >"$dir/commit"
 printf '%s\n' "${labels[@]}" >"$dir/targets"
 args=(test "${common[@]}" --keep_going --strategy=TestRunner=local --nocache_test_results --test_sharding_strategy=disabled --test_output=errors --test_arg=-test.v "--test_filter=$filter" --nobuild_event_json_file_path_conversion --build_event_publish_all_actions "--build_event_json_file=$RUNNER_TEMP/$arm.bep" "${labels[@]}")
 printf '%s\n' "${args[@]}" >"$dir/arguments"
 base=$("${baz[@]}" info output_base)
 result=0; "${baz[@]}" "${args[@]}" >"$RUNNER_TEMP/$arm.log" 2>&1 || result=$?
 printf '%d\n' "$result" >"$dir/exit"
 date -u +%FT%T.%3NZ >"$dir/finish"
 git rev-parse HEAD >"$dir/head"; git status --porcelain --untracked-files=no >"$dir/after"
 prune "$RUNNER_TEMP/$arm.log"
 cap=0; python3 -B "$tools/qualification_report.py" capture --events "$RUNNER_TEMP/$arm.bep" --output "$dir/events.jsonl" --output-root "$base" --report-dir "$dir/original" || cap=$?
 echo "$cap" >"$dir/capture-exit"
 python3 -B "$tools/state_result.py" context "$dir" "$src"
 python3 -B "$tools/qualification_report.py" verdict --events "$dir/events.jsonl" --context "$dir/context.json" --expected-targets "$dir/targets" --output "$dir/verdict.json" >/dev/null || true
 "${baz[@]}" build "${common[@]}" //pkg/state/tests:tests_test >"$RUNNER_TEMP/$arm-build.log" 2>&1 || { prune "$RUNNER_TEMP/$arm-build.log"; exit 1; }
 "${baz[@]}" cquery "${common[@]}" --output=files //pkg/state/tests:tests_test >"$dir/binpaths" 2>"$RUNNER_TEMP/$arm-cquery.log"
 mapfile -t bins < <(while IFS= read -r path; do if [[ -f $path && -x $path ]]; then echo "$path"; fi; done <"$dir/binpaths")
 test "${#bins[@]}" -eq 1
 cp "${bins[0]}" "$RUNNER_TEMP/graph-$arm"
 sha256sum "$RUNNER_TEMP/graph-$arm" | cut -d' ' -f1 >"$dir/bin.sha256"
)
set +e
# A failing arm does not suppress the other source's correctness evidence.
prepare control 6e1c4aad76d023a5313f00625604f01b5705dc8b; control_rc=$?
prepare candidate 0ca688f73ed901aa29252544380cc5242c4fef2a; candidate_rc=$?
set -e
printf 'control=%d\ncandidate=%d\n' "$control_rc" "$candidate_rc" >"$out/build-status"
if (( !control_rc && !candidate_rc )); then
 cpu=$(python3 -c 'import os;print(min(os.sched_getaffinity(0)))')
 { uname -srmo; ls -1 /sys/devices/system/cpu | head -0 || true; lscpu | grep -E '^(Model name|Architecture):'; printf 'pinned CPU: %s; GOMAXPROCS=1; benchtime=250ms\n' "$cpu"; } >"$out/native"
 for i in $(seq 1 12); do
  if (( i%2 )); then order=(control candidate); else order=(candidate control); fi
  for arm in "${order[@]}"; do
   printf '%s %s\n' "$i" "$arm" >>"$out/order"
   GOMAXPROCS=1 taskset -c "$cpu" "$RUNNER_TEMP/graph-$arm" -test.run='^$' '-test.bench=^(BenchmarkSparseIndexSave|BenchmarkManyWideIndexSave|BenchmarkGraphSave)$' -test.benchtime=250ms -test.count=1 -test.benchmem >"$out/$arm/round-$i.log" || break 2
   grep '^Benchmark' "$out/$arm/round-$i.log" >>"$out/$arm.bench"
  done
 done
fi
if [[ -s "$out/control.bench" && -s "$out/candidate.bench" ]]; then
 (
  set -e
  tool=2b51abee8530385431b7225198fdd5cd65e78696
  git fetch -q --depth=1 origin "$tool"
  git checkout -q --detach "$tool"
  tgt=@org_golang_x_perf//cmd/benchstat
  "${baz[@]}" build "${common[@]}" "$tgt" >"$RUNNER_TEMP/benchstat.log" 2>&1 || { prune "$RUNNER_TEMP/benchstat.log"; exit 1; }
  "${baz[@]}" cquery "${common[@]}" --output=files "$tgt" >"$RUNNER_TEMP/tool-files" 2>"$RUNNER_TEMP/tool-query.log"
  mapfile -t bins < <(while IFS= read -r p; do if [[ -f $p && -x $p ]]; then echo "$PWD/$p"; fi; done <"$RUNNER_TEMP/tool-files")
  [[ ${#bins[@]} == 1 ]]; (cd "$out"; "${bins[0]}" control.bench candidate.bench >benchstat.txt)
 ) || true
fi
python3 -B "$tools/graph_pair_verdict.py" "$out"
