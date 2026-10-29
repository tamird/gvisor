#!/bin/bash
set -euo pipefail
work=$1
source=$2
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
git checkout --quiet --detach "$source"
[[ $(git rev-parse HEAD) == "$source" ]]
mkdir -p "$work/replay-private" "$work/replay-public"
overall=0
for lane in normal race amd64-check arm64-check; do
  dir="$work/replay-private/$lane"; mkdir -p "$dir"
  git status --porcelain --untracked-files=no >"$dir/source_before"
  git cat-file commit HEAD >"$dir/commit_file"
  arch=x86_64; args=()
  case "$lane" in
    normal|race)
      targets=(//pkg/state:state_test //pkg/state/tests:tests_test //pkg/state/wire:wire_test)
      args+=(--test_env=GO_TEST_WRAP_TESTV=1 --test_arg=-test.v)
      [[ $lane != race ]] || args+=(--config=race) ;;
    *-check)
      targets=(//pkg/state:state_nogo //pkg/state:state_test_nogo //pkg/state/pretty:pretty_nogo //pkg/state/wire:wire_nogo //pkg/state/wire:wire_test_nogo //pkg/state/tests:tests_nogo //pkg/state/tests:tests_test_nogo)
      [[ $lane != arm64-check ]] || arch=aarch64 ;;
  esac
  raw="$dir/private-bep.jsonl"
  cmd=(test --keep_going "--config=$arch" -c opt --jobs=80 --test_timeout=360 --nocache_test_results '--modify_execution_info=^TestRunner$=+no-remote-cache' --remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$' --test_sharding_strategy=disabled "--build_event_json_file=$raw" --build_event_json_file_path_conversion=false "${args[@]}" "${targets[@]}")
  printf '%s\n' "${cmd[@]}" >"$dir/executed_arguments"
  code=0
  bazel --bazelrc="$work/poll-gnu.bazelrc" "${cmd[@]}" >"$dir/bazel.log" 2>&1 || code=$?
  date -u '+%Y-%m-%dT%H:%M:%S.%3N+00:00' >"$dir/finished"
  printf '%s\n' "$code" >"$dir/native_exit"; cp "$dir/native_exit" "$dir/runner_exit"
  git rev-parse HEAD >"$dir/final_head"; git status --porcelain --untracked-files=no >"$dir/source_after"
  python3 -B "$work/checker/tcp_long_replay.py" "$dir" "$lane" "$source" "${targets[*]}" "$raw" "$work/replay-public" || overall=1
done
python3 - "$work/replay-public" "$source" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
lanes=['normal','race','amd64-check','arm64-check']
receipts={lane:json.loads((root/(lane+'.json')).read_text()) for lane in lanes if (root/(lane+'.json')).is_file()}
missing=[a for a in lanes if a not in receipts]
ok=not missing and all(r['verified'] and r['source']==sys.argv[2] for r in receipts.values())
summary={'status':'PASS' if ok else 'FAIL','source':sys.argv[2],'expected':lanes,'executed':list(receipts),'missing':missing}
(root/'summary.json').write_text(json.dumps(summary)+'\n')
print(summary)
sys.exit(not ok)
PY
exit "$overall"
