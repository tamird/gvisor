# Generated-source coverage checks

Run the coverage selection, generated-code execution and static inventory checks
on a Linux AMD64 worker:

```sh
bazel test --keep_going --config=x86_64 \
  //tools/bazeldefs/tests/coverage:selection_test \
  //tools/bazeldefs/tests/coverage/probe:protobuf_coverage_test \
  //tools/bazeldefs/tests/coverage:inventory_test
```

This test builds the declared Sentry coverage binary and runs `symbolize -all`.
It validates every PC/position pair, requires unique PCs and positive ordered
source coordinates, requires Sentry kernel and generated protobuf positions,
and enforces coverage-runtime and platform exclusions. Counts are reported as
JSON. The target is manual because it builds the complete coverage binary.

This checks the current inventory's structure, not parity with a legacy build.
The migration's complete 115841-position legacy inclusion check (including
2418 protobuf positions) passed on matched source
`1f33fe3003455db178b97f3b43d3a012f1611994` in run 44467492263. That result and
the source-bound fixture remain in the qualified history at `f22940365a`.
Upstream source edits change coordinates, so that historical inventory is not
a golden for current source. Current-source legacy parity requires inventories
from both implementations on matched source. Static inventories do not establish
executed coverage, syscall-runner output ownership or performance.

The analysis test checks real `GoInfo` source and coverage sets under ordinary,
selected, excluded and mixed embedding configurations. Embedded data stays
outside coverage.

The NORMAL-mode executable uses the production `go_cov` rule. It clears
initialization counters, calls a generated protobuf getter with a runtime value,
and requires that source in the executed coverage report. It also checks that
the coverage runtime remains excluded.
