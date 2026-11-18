Check the exact three-commit stack against the union of prior qualified inputs.
All six changed Go blobs are identical to their qualified originals. The full
tools-owner expected XML set is the exact 29-name union of prior Iperf24 and
Hey13 results; no failure/error/skip or extra/missing case can pass. Build all
four Hey/network consumer binaries. These are native AMD64 helper checks, not
runtime or performance qualification.

Retain original old-helper/readiness negatives and source/full AMD C++ lint
evidence. Lint tool/config/module inputs are unchanged between original bases;
all extra Go blobs passed those same source checks, and stacking changes no C++
input. Do not rerun these checks solely for stacking. Expected observed build
cost is approximately 7–12 minutes if comparable to prior 102.882s tools-owner
preparation and 290.409s four-consumer build; cold-host variation is unknown.
Hard limit 58 minutes plus 120s cleanup grace = 60 minutes inclusive. Cleanup
is source-first, signal-safe and bounded 57s. No images, full release build,
workload, negative-control repetition, candidate runtime or statistical study.
