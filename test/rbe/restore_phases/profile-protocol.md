Before inspecting any samples, fix 192 fresh CPU-profiled benchmark processes
and eight matched allocation/empty-bracket pairs. Every process uses the same
in-job capture and must restore the CPUID and kernel twice. The raw
checkpoint and raw pprof files stay on the worker.

CPU profiling covers the lifetime of the benchmark using Go's normal sampler.
Only the two kernel state.Load calls carry a cold/warm label. Report each
period and every sample, including unlabeled samples and samples under a
label without a state.Load frame, separately from samples with both. Include
per-process counts, unknown leaves, and both flat and inclusive attribution.
A cold group with fewer than 50 matched samples is directional at best;
the presence of a symbol on a stack does not establish a change will help.

Allocation workers use MemProfileRate=1 and snapshot the cumulative allocs
profile immediately before and after each kernel Load, with two explicit GCs
before each snapshot to expose older samples. Matched fresh blank workers
do the same snapshots surrounding no work, then actually Load the same
kernel. Retain signed differences, both object counts and space, and separate
stacks containing all of the kernel benchmark/pprof.Do/state.Load from other
or truncated stacks. No latency claim is based on the instrumented runs.
