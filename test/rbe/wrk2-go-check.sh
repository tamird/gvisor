set -euo pipefail
r="$RUNNER_TEMP/wrk2/consumer"; mkdir -p "$r"; exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach 46c4aab9a3cc572b0554bb60b5e9d6080b4c1aca
git rev-parse HEAD >"$r/source"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
label=//test/kubernetes/benchmarks/httpbench:wrk2_test
bz test --config=x86_64 --test_arg=-test.v --nocache_test_results --test_output=all "$label"
cp "$(bz info bazel-testlogs)/test/kubernetes/benchmarks/httpbench/wrk2_test/test.log" "$r/test.log"
grep -F -- '--- PASS: TestWrk2Consumer' "$r/test.log"
date -u '+PASS %FT%TZ' >"$r/verdict"
