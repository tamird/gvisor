#!/bin/bash
set -euo pipefail
work=$1 lane=$2 source=$3
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
[[ $(git rev-parse HEAD) == "$source" ]]
d=$work/public/$lane
mkdir -p "$d"
checker=$work/array-tools/qualification_report.py
git cat-file -p HEAD >"$d/commit.txt"
git status --porcelain --untracked-files=no >"$d/source-before.txt"
test ! -s "$d/source-before.txt"
arch=x86_64
args=(); targets=(//pkg/state/tests:tests_test)
case "$lane" in
 normal|race)
   args+=(--test_env=GO_TEST_WRAP_TESTV=1 --test_arg=-test.v)
   [[ $lane != race ]] || args+=(--config=race);;
 arm)
   arch=aarch64
   targets=(//pkg/state:state_nogo //pkg/state:state_test_nogo //pkg/state/pretty:pretty_nogo //pkg/state/wire:wire_nogo //pkg/state/wire:wire_test_nogo //pkg/state/tests:tests_nogo //pkg/state/tests:tests_test_nogo)
   cp "$work/array-tools/filter-interpreter.txt" "$d/filter-interpreter.txt";;
esac
printf '%s\n' "${targets[@]}" >"$d/targets.txt"
cmd=(test "--config=$arch" -c opt --jobs=12 --keep_going --test_timeout=1200 --nocache_test_results --strategy=TestRunner=standalone '--modify_execution_info=^TestRunner$=+no-remote-cache' --remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$' --test_sharding_strategy=disabled "--build_event_json_file=$work/private/$lane.bep" --build_event_json_file_path_conversion=false "${args[@]}" "${targets[@]}")
printf '%s\n' "${cmd[@]}" >"$d/arguments.txt"
code=0;capture=0;verify=0
bazel "--bazelrc=$work/array.bazelrc" "${cmd[@]}" >"$work/private/$lane.bazel-log" 2>&1 || code=$?
date -u +%Y-%m-%dT%H:%M:%S.%3N+00:00 >"$d/finished.txt"
echo "$code" >"$d/native-exit.txt"
git rev-parse HEAD >"$d/final-head.txt"
git status --porcelain --untracked-files=no >"$d/source-after.txt"
python3 -B "$checker" capture --events "$work/private/$lane.bep" --output "$d/events.jsonl" --output-root "$PWD/bazel-out" --report-dir "$d/reports" || capture=$?
echo "$capture" >"$d/capture-exit.txt"
python3 - "$d" "$source" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
d=dict(expected_source=sys.argv[2],command_kind="test",execution_finished_at=(p/"finished.txt").read_text().strip(),commit_file="commit.txt",source_before="source-before.txt",source_after="source-after.txt",final_head="final-head.txt",executed_arguments="arguments.txt",native_exit="native-exit.txt",capture_exit="capture-exit.txt")
(p/"context.json").write_text(json.dumps(d)+"\n")
PY
python3 -B "$checker" verdict --context "$d/context.json" --events "$d/events.jsonl" --manifest "$d/events.jsonl.manifest.json" --expected-targets "$d/targets.txt" --output "$d/verdict.json" || verify=$?
python3 - "$d/verdict.json" <<'PY'
import json,sys
x=json.load(open(sys.argv[1]));print({k:x[k] for k in ("source","status","case_counts","missing","native_exit","execution_to_verdict_seconds")})
if x["case_counts"]["passed"]==0:sys.exit(1)
PY
test "$code" -eq 0 && test "$capture" -eq 0 && test "$verify" -eq 0
