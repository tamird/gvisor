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
  digest=$(sha256sum "$dir/$arm.bin" | cut -d " " -f 1)
  printf '%s %s %s %s %s\n' "$arm" "$(git rev-parse HEAD^)" "$sha" "$q" "$digest" >> "$proof/source-arms.txt"
done
git checkout --quiet --detach "$wrapper"
mv "$dir/before.bin" "$dir/after.bin" test/rbe/array_pair/
cp "$work/nginx.tar" test/rbe/array_pair/nginx.tar
git cat-file commit HEAD >"$dir/commit_file"
git status --porcelain --untracked-files=no >"$dir/source_before"
raw=$dir/private-bep.jsonl
args=(test --config=x86_64 -c opt --jobs=80 --test_timeout=1200 --nocache_test_results '--modify_execution_info=^TestRunner$=+no-remote-cache' --remote_download_outputs=toplevel --zip_undeclared_test_outputs '--remote_download_regex=.*[/](test[.](log|xml)|outputs[.]zip|before[.]bench|after[.]bench|fixture[.]sha256|binaries[.]sha256|order[.]txt)$'  --build_event_json_file_path_conversion=false "--build_event_json_file=$raw" "$target")
printf '%s\n' "${args[@]}" >"$dir/executed_arguments"
code=0
"${baz[@]}" "${args[@]}" >"$dir/bazel.log" 2>&1 || code=$?
date -u '+%Y-%m-%dT%H:%M:%S.%3N+00:00' >"$dir/finished"
echo "$code" >"$dir/native_exit"; cp "$dir/native_exit" "$dir/runner_exit"
git rev-parse HEAD >"$dir/final_head"
git status --porcelain --untracked-files=no >"$dir/source_after"
# Keep only the allowlisted outputs; the full event stream contains private flags.
python3 - "$raw" "$proof" <<'META'
import json, pathlib, sys
events, proof=map(pathlib.Path, sys.argv[1:])
allowed={"outputs.zip","before.bench","after.bench","order.txt","fixture.sha256","binaries.sha256"}
records=[]
if events.is_file():
    for line in events.open():
        row=json.loads(line)
        if "testResult" not in row:
            continue
        for output in row["testResult"].get("testActionOutput", []):
            name=output.get("name","")
            if pathlib.Path(name).name in allowed:
                records.append({field:output[field] for field in ("name","digest","length") if field in output})
(proof/"output-records.json").write_text(json.dumps(records)+"\n")
META
base=bazel-testlogs/test/rbe/array_pair/checkpoint/test.outputs
for file in before.bench after.bench order.txt fixture.sha256 binaries.sha256; do
  if [[ -f $base/outputs.zip ]] && unzip -p "$base/outputs.zip" "$file" >"$dir/$file.tmp"; then
    cp "$dir/$file.tmp" "$proof/$file"
  elif [[ -f $base/$file ]]; then
    cp "$base/$file" "$proof/$file"
  fi
done
verification=0
python3 -B "$work/checker/tcp_long_replay.py" "$dir" pair "$wrapper" "$target" "$raw" "$proof" || verification=$?
if (( code != 0 )); then
  grep -E '^(ERROR:|FAIL:|INFO: Streaming)' "$dir/bazel.log" | tail -10 || true
  test ! -r bazel-testlogs/test/rbe/array_pair/checkpoint/test.log || tail -40 bazel-testlogs/test/rbe/array_pair/checkpoint/test.log
  exit "$code"
fi
test "$verification" -eq 0
for file in before.bench after.bench order.txt fixture.sha256 binaries.sha256; do test -s "$proof/$file"; done
python3 - "$proof" <<'VERIFY'
import pathlib, sys
p=pathlib.Path(sys.argv[1])
arms={a[0]:a[-1] for a in (row.split() for row in (p/"source-arms.txt").read_text().splitlines())}
binary={}
for line in (p/"binaries.sha256").read_text().splitlines():
    digest,path=line.split()
    binary[pathlib.Path(path).stem]=digest
if binary!=arms or len(set(binary.values()))!=2:
    raise SystemExit(f"Arm binary mismatches: {arms}, {binary}")
for arm in arms:
    if len((p/f"{arm}.bench").read_text().splitlines())!=32:
        raise SystemExit(f"Incomplete paired measurements: {arm}")
VERIFY
wc -l "$proof/"*.bench
