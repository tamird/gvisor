#!/bin/bash
set -euo pipefail
work=$1
control=$2
treatment=$3
wrapper=$4
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
q=//test/benchmarks/checkpointfile:checkpointfile_test
target=//test/rbe/array_pair:checkpoint
dir=$work/paired-private
proof=$work/paired-public
mkdir -p "$dir" "$proof"
baz=(bazel "--bazelrc=$work/poll-gnu.bazelrc")
for arm in before after; do
  sha=$control; [[ $arm == before ]] || sha=$treatment
  git checkout --quiet --detach "$sha"
  [[ "$(git rev-parse HEAD)" == "$sha" && -z "$(git status --porcelain --untracked-files=no)" ]]
  "${baz[@]}" build -c opt --config=x86_64 --jobs=80 --remote_download_outputs=toplevel "$q" >"$dir/$arm-build.log" 2>&1 || { grep -E '^(ERROR:|INFO: Streaming)' "$dir/$arm-build.log" | tail -12; exit 1; }
  "${baz[@]}" cquery -c opt --config=x86_64 --output=files "$q" >"$dir/$arm-files" 2>"$dir/$arm-query.log"
  mapfile -t paths < <(while IFS= read -r file; do [[ ! -f $file || ! -x $file ]] || echo "$file"; done <"$dir/$arm-files")
  [[ ${#paths[@]} -eq 1 ]] || { cat "$dir/$arm-files"; exit 1; }
  readelf -h "${paths[0]}" | grep -q 'Advanced Micro Devices X86-64'
  cp -L "${paths[0]}" "$dir/$arm.bin"
  printf '%s %s %s\n' "$arm" "$(git rev-parse HEAD^)" "$sha" >> "$proof/source-arms.txt"
done
git checkout --quiet --detach "$wrapper"
mv "$dir/before.bin" "$dir/after.bin" test/rbe/array_pair/
cp "$work/nginx.tar" test/rbe/array_pair/nginx.tar
git cat-file commit HEAD >"$dir/commit_file"
git status --porcelain --untracked-files=no >"$dir/source_before"
raw=$dir/private-bep.jsonl
args=(test --config=x86_64 -c opt --jobs=80 --test_timeout=1200 --nocache_test_results '--modify_execution_info=^TestRunner$=+no-remote-cache' --remote_download_outputs=toplevel '--remote_download_regex=.*[/](test[.](log|xml)|outputs[.]zip)$' --build_event_json_file_path_conversion=false "--build_event_json_file=$raw" "$target")
printf '%s\n' "${args[@]}" >"$dir/executed_arguments"
code=0
"${baz[@]}" "${args[@]}" >"$dir/bazel.log" 2>&1 || code=$?
date -u '+%Y-%m-%dT%H:%M:%S.%3N+00:00' >"$dir/finished"
echo "$code" >"$dir/native_exit"; cp "$dir/native_exit" "$dir/runner_exit"
git rev-parse HEAD >"$dir/final_head"
git status --porcelain --untracked-files=no >"$dir/source_after"
verification=0
python3 -B "$work/checker/tcp_long_replay.py" "$dir" pair "$wrapper" "$target" "$raw" "$proof" || verification=$?
if (( code != 0 )); then
  grep -E '^(ERROR:|FAIL:|INFO: Streaming)' "$dir/bazel.log" | tail -10 || true
  test ! -r bazel-testlogs/test/rbe/array_pair/checkpoint/test.log || tail -40 bazel-testlogs/test/rbe/array_pair/checkpoint/test.log
  exit "$code"
fi
test "$verification" -eq 0
zip=bazel-testlogs/test/rbe/array_pair/checkpoint/test.outputs/outputs.zip
for file in before.bench after.bench order.txt fixture.sha256 binaries.sha256; do unzip -p "$zip" "$file" >"$proof/$file"; test -s "$proof/$file"; done
wc -l "$proof/"*.bench
