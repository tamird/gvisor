# Retained qualification report fixtures

These are small projections of original Actions artifacts from October 16, 2026.
Each `provenance.json` records the source commit, run/archive identities, original
body hashes, projection rule, and retained log offsets. XML is unchanged. BEP
report URIs are relocated by the test; summary URI lists are omitted because the
capture owner uses each `testResult`'s original reports.

* `pass`: run 38934559463, one native exec owner, all 49 named cases passing.
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

Other tests deliberately mutate these inputs to exercise source/configuration
mismatches, missing results, report corruption, resumable capture, wrapper errors,
and exit-status precedence. Those mutations are regression inputs, not claims
about the original runs. The tests run through the repository's Bazel target.
