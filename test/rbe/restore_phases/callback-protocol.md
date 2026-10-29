Use one stock runsc capture for every arm and require matching CPUID/kernel
frame bytes, task identities and CPU feature bits after both restores. Before
the timed pairs, run the existing shape reporter in its own fresh process on
that capture. It records kind, size, pooled capacity and actual growth steps;
its output is not a timing sample.

Run 16 fresh-process pairs, alternating the arm that starts first. Compare
launch to first verified restore, kernel first Load, and kernel warm Load.
Then run eight independent fresh-process pairs with process-wide heap counters
bracketing just kernel Load. Do not force GC; allocation workers are excluded
from timing summaries. Preserve all pair deltas and report their distributions
rather than a p-value. This is one graph on one worker; it does not establish
how to predict callback demand for arbitrary checkpoints.
