# Generated-source coverage checks

Run both checks on a Linux AMD64 worker:

```sh
bazel test --keep_going --config=x86_64 \
  //tools/bazeldefs/tests/coverage:selection_test \
  //tools/bazeldefs/tests/coverage/probe:protobuf_coverage_test
```

The migration's full static inventory check is separate:

```sh
bazel test --keep_going --config=x86_64 \
  //tools/bazeldefs/tests/coverage:legacy_inventory_test
```

This test builds the declared Sentry coverage binary and runs `symbolize -all`.
It checks complete inclusion of the 115841 positions (including 2418 generated
protobuf positions) retained from source `1f33fe3003455db178b97f3b43d3a012f1611994`,
run 44254336532, artifact 14406811432. The compressed and plain inventories are
content verified. Additional positions are permitted; coverage-runtime and
platform exclusions remain enforced.

The target is manual because this is a source-bound migration baseline. Later
source changes require an explicit decision about removed positions; do not
silently regenerate the fixture. This static comparison does not establish
executed coverage, syscall-runner output ownership or performance.

The analysis test checks real `GoInfo` source and coverage sets under ordinary,
selected, excluded and mixed embedding configurations. Embedded data stays
outside coverage.

The NORMAL-mode executable uses the production `go_cov` rule. It clears
initialization counters, calls a generated protobuf getter with a runtime value,
and requires that source in the executed coverage report. It also checks that
the coverage runtime remains excluded.
