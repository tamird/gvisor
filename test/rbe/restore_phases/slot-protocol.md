Predeclared 2026-10-29 05:36:30 UTC, before any timing was collected.
Capture one unmodified Nginx state, then run exactly 16 consecutive pairs
of fresh children. Odd pairs start stock; even pairs start candidate. Within
each pair both restore the same CPUID/kernel frames two times, verify task
IDs/names and CPUID feature mask, and keep the original raw benchmark rows.
A single parent uses CLOCK_MONOTONIC from immediately before starting the
child executable until reading its first completed restore line. This
includes process startup, fixture setup, verification, and pipe notification;
it is not the time inside state.Load. Report actual first and second
CPUID and kernel Load durations separately, and their sums. Record per-arm
median and median within-pair difference, direction counts and dispersion.
One worker/capture, no outlier exclusion or samples added after the result.
Exclude type-counter and profiling hooks from both binaries; do not
attribute a net difference to the 541 wrappers individually.
