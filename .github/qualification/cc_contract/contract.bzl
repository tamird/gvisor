"""Exercise the existing high-level cc_test macro on the native host."""

load("@rules_cc//cc:defs.bzl", "cc_test")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

configured_test, _configured_transition = with_cfg(cc_test).extend("copt", ["-DCC_CONTRACT_CONFIGURED=1"]).build()
