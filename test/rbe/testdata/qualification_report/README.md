# Retained qualification report fixtures

These are small projections of original Actions artifacts.
Each `provenance.json` records the source commit, run/archive identities, original
body hashes, projection rule, and retained log offsets. XML is unchanged. BEP
report URIs are relocated by the test; summary URI lists are omitted because the
capture owner uses each `testResult`'s original reports.

* `pass`: run 38934559463, one native exec owner, 45 passing cases and four skips.
* `fail`: run 38941203981, shard 8 of the container systrap owner. Its 64 XML
  entries include a fixture-creation failure. The original eight-shard
  announcement/summary remains, deliberately making the other seven shards
  missing in this projection. The log is its original final 16 KiB.
* `incomplete`: run 38940974369 stopped during dependency analysis, before the
  capture wrapper produced a BEP or command body. The empty events preserve
  that absence; the 34 expected owners come from the retained reconciliation.
  The commit body comes from another bucket at the identical source. The
  fixture's `test` argument and capture exit 1 declare absent evidence rather
  than inventing original command/capture records.
* `zero`: run 38951386329, the filtered serialization wrapper returned native
  success and an empty XML suite. This is not named test coverage.
* `analysis-no-build`: the bounded reporting probe's original `test --nobuild`
  events and native exit 1. Both configured owners finish through `NO_BUILD`,
  with no analysis failure or execution. The test checks this complete pattern
  and rejects missing owners, other aborts, and unrelated nonzero exits.
* `aspect-analysis`: run 39111747901 successfully analyzed the standalone TUN
  target and its ClangTidy aspect. The shared owner parser rejected their
  distinct events as duplicate targets, so no lint action executed. These
  unchanged analysis events test that distinction; build-verdict variants
  replace `NO_BUILD` with explicit success/failure or missing completions.
* `filtered-save`: invocation e1d9f7dd-28c6-4cab-a212-9cb84868a94a built
  `32bit_test_native` but its `save_restore` filter executed only
  `32bit_test_runsc_systrap_directfs_save`. Both remain in the actual BEP.
  Expected analysis is projected to the selected owner for this regression;
  it is not evidence of running the corrected `--build_tests_only` preflight.
* `benchmark`: the original `slice_diagnostic_empty` result at ce3880486 has
  native/BEP success, one numeric row and two synthetic XML errors from verbose
  benchmark RUN markers. Its original XML and log suffix remain unchanged.
  Tests explicitly project nonverbose output, empty XML and future arguments
  to exercise benchmark reporting; this projection is not another measurement
  or proof of the real wrapper behavior.
  `docker-stderr.txt` is the unchanged stderr suffix from PGO KVM run
  39092637450's ffmpeg owner. Bazel appended the Docker image-pull messages
  after the benchmark's stdout; its separate provenance records that artifact.
* `node-tap`: two unchanged batch artifacts from invocation
  `4ae56120-c5a2-451f-8334-5103d32be7d8`. Batch 150 completed all 50 selected
  cases, including HTTP cork. Batch 0 completed 49 of 50 selections, with five
  explicit skips; the remaining name was a recursively discovered fixture
  outside Node's canonical suites. Both include the real success footer.
  The original aggregate remains failed; the fixture must stay unaccounted.
* `source-images`: one local Docker iperf owner from run 39531331807, with
  its uncached remote source producer and stock archive conversion. The raw
  compact projection keeps the invocation header, original input IDs and
  accepted ancestry edges, including the runfiles tree. Unrelated edges and
  directory contents are omitted. XML is unchanged; the log retains load lines, numeric measurements
  and the package PASS marker. The canonical verdict is projected to this
  configured owner. Local TestRunner digest absence is original evidence.

Other tests deliberately mutate these inputs to exercise source/configuration
mismatches, missing results, report corruption, resumable capture, wrapper errors,
and exit-status precedence. Those mutations are regression inputs, not claims
about the original runs. The tests run through the repository's Bazel target.
