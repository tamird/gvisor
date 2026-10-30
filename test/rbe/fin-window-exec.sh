#!/bin/bash
set -euo pipefail
phase=$1; shift
rec=$RUNNER_TEMP/records
d=$rec/$phase; mkdir -p "$d"
printf '%s\n' "$CURRENT" > "$d/source"
args=(--config=x86_64 --jobs=6 --test_sharding_strategy=disabled --nocache_test_results --keep_going --strategy=TestRunner=local --local_test_jobs=1 --strategy=GoStaticAnalysis=linux-sandbox --strategy=GoStandardLibraryAnalysis=linux-sandbox --strategy=GoStdlib=linux-sandbox --strategy=GoStdlibList=linux-sandbox '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache' --remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$' --build_event_json_file_path_conversion=false "--build_event_json_file=$d/raw.jsonl" --test_env=XTABLES_LOCKFILE --test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated --define=gvisor_test_clock_source=calibrated --test_timeout=600 --test_output=errors)
printf '%s\n' "${args[@]}" "$@" > "$d/arguments"
rc=0
docker exec -e XTABLES_LOCKFILE="$RUNNER_TEMP/xtables.lock" tcp-compiler bazel --bazelrc="$RUNNER_TEMP/tcp.bazelrc" test "${args[@]}" "$@" > "$RUNNER_TEMP/fin-$phase.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$d/exit"
grep -A2 -E 'Invocation ID:|ERROR:|FAILED| error: |FAILED_TO_BUILD| failed: |Could not| --- FAIL:' "$RUNNER_TEMP/fin-$phase.log" | head -180 > "$d/diagnostics" || true
result=0
docker exec tcp-compiler python3 test/rbe/fin-window-report.py "$phase" "$rec" "$CURRENT" "$rc" "$@" || result=$?
sudo mv "$d/raw.jsonl" "$RUNNER_TEMP/private-$phase-bep.jsonl" || :
exit "$result"
