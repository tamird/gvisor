# Explicit PGO consumption qualification

These six profiles were produced by the Go 1.27.3 AMD64 systrap workload
owners recorded in `provenance.json`. The failed original Ruby result is
preserved; its replacement comes from the separately successful FUSE
readiness recovery. No producer is rerun by this package.

The existing profiletool merges raw samples without benchmark reweighting.
Select the resulting file through the native rules_go setting:

`--@io_bazel_rules_go//go/config:pgoprofile=//test/rbe/pgo_consumer:merged_profile`

Use that same setting for `//test/rbe/pgo_consumer:release` and
`//:release_smoke_test`. The former exposes the public `//:release`
outputs to the existing qualification recorder. The smoke test executes
that release layout, including the separate Sentry binary.

This opt-in qualification does not change default production builds or
claim equal workload weighting, performance improvement, ARM support, or
KVM coverage.
