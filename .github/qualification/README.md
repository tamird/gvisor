Matched iperf component study, pending independent review/publication

SOURCE f9446ddf973def4ce653acbb098532c2b1eb0ef0; exact BASE
1f33fe3003455db178b97f3b43d3a012f1611994. AMD64 only. No source behavior edits,
new VM, credentials, tool home or alternate Bazel output base. Root approved
24 samples per source for this component; other networking/loss gates stay open.

Reuse the reviewed systrap-samepath fileset.py, quiescent.py and docker_cleanup.py
unchanged. freeze_harness.py has only one generalization: executable names are
explicit trailing arguments, here exactly iperf_test. Preserve both complete
canonical Make/Bazel //:release filesets, archives and full-member fingerprints.
Build one static BASE benchmark harness and materialize its whole runfiles tree.
One BASE harness, one unchanged Docker runtime configuration, one actual active
fileset directory, one iperf image ID, and one checkout revision during visits.
Every visit recreates that same active path and copies the selected ENTIRE
frozen fileset. Check release/harness hashes, modes, image ID, runtime config and
process/container quiescence before/after. No project build or source switch
occurs during the measurement phase. Cleanup restores daemon config bytes/mode/
owner or absence, removes only task runtime containers and restores SOURCE.

Exactly one full discarded warmup per source, then predetermined alternating
AB/BA pairs1..24. Every measured visit executes all eight unchanged iperf
variants once at1048576x (1GiB nominal aggregate per variant;16MiB per connection
at parallel64), -test.cpu=4/GOMAXPROCS=4. No A/A or extra matrix. Direct-host
execution uses the exact canonical benchmark_test ELF and complete runfiles;
this is the execution path documented by test/benchmarks/defs.bzl. Record the
owning Make/rule sources and harness BASE separately from each runtime source.

Strict accounting requires all8 names exactly once per visit, exact N, positive
finite metrics, same host headers, matching before/after frozen filesets, and
24 measured samples per source/case. Warmups remain separate. Pinned external
benchstat4febdb082b2ce2a0f9f6f496698aa5a2273279b8 is installed outside checkout;
record host Go and go version-m. Retain every raw log and full raw benchstat
output. Interpret ns/op as including canonical container startup; the helper's
SetBytes-derived MB/s normalization differs from its custom iperf bandwidth.
No significance claim precedes actual complete data. Hosted-runner variability
can leave wide confidence intervals even with24 samples.

Actual preceding pilot44213001194 used the same volume: release builds681s,
whole-category visits34..41s. Approximate study cost~42min before extra frozen
fileset verification/setup. Individual visit180s includes a150s benchmark cap;
50 total visits are at most150min. Global195min deadline plus at most180s
termination/restoration grace bounds the entire plan below198min, even if
individual preparation caps would otherwise sum higher. Generic job330min.
Candidate release5min cap vs previous measured105s; BASE15min vs measured576s.
No partial sample set or deadline exhaustion can pass the study.

Readiness correction: idle-daemon bounded restart follows registration, then the
reviewed coverage readiness helper (single-runtime expectedname adaptation)
requires daemon configuration AND Dockerinfo exact active/runsc path within60s
using monotonic deadlines. Absence fails before any warmup. No reload-success
assumption is used. Cleanup restores the original configuration through the
unchanged reviewed helper. The195m global deadline remains the hard cap.
