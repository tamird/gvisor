# Generated-source coverage checks

Run both checks on a Linux AMD64 worker:

```sh
bazel test --keep_going --config=x86_64 \
  //tools/bazeldefs/tests/coverage:selection_test \
  //tools/bazeldefs/tests/coverage/probe:protobuf_coverage_test
```

The analysis test checks real `GoInfo` source and coverage sets under ordinary,
selected, excluded and mixed embedding configurations. Embedded data stays
outside coverage.

The NORMAL-mode executable uses the production `go_cov` rule. It clears
initialization counters, calls a generated protobuf getter with a runtime value,
and requires that source in the executed coverage report. It also checks that
the coverage runtime remains excluded.
