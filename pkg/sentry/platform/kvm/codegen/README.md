# Atomic pointer caller gate prototype

`//pkg/sentry/platform/kvm:atomicptr_codegen_test` inspects the actual production
Sentry ELF and kernel/KVM package archives on amd64 and arm64. It does not run
KVM or Sentry. The data transition selects ordinary optimized, non-race builds
with DWARF retained by built-in `--strip=never`; it does not use `-c dbg` or
change positive compilation options.

Live DWARF records must connect each selected source call to its generated
wrapper. Logical inline ownership is retained when the containing function is
itself inlined into another concrete ELF function. Missing origins, source
positions or live ranges fail the test. An absent CALL is not positive evidence.
The source positions come from the actual production source inputs.

The normal production Sentry link retains Go's NOSPLIT stack-chain checks.
The test also checks the relevant source directives and inspects scoped archive
calls through checkescape's existing SDK objdump reader. A test-only library
embeds the complete real KVM package with `gc_goopts = ["-l"]`. Its actual signal
handler must retain the expected machine pointer Load call and be rejected by
the same residual-call validator. No whole-function instruction golden is kept.

This is an unqualified prototype. The first bounded remote run must establish
that the configured rules_go providers and Go SDK tools work as specified and
that these particular DWARF records survive production linking. Synthetic unit
cases do not prove those facts. Formatting, builds, tests and CI cost have not
been checked locally. JSON reports retain scoped records and archive identities
on failure; the qualification plan must retain declared production outputs for
any deeper diagnosis. No performance, stack-size neutrality or complete-delivery
claim follows from passing this gate. Compare current-source instructions,
relocations and frames separately as one-time qualification evidence.
