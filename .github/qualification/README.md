Same-path systrap benchmark qualification (not launched)

Bind SOURCE5a397632d896158997ee7594866b33d741c56917 and its exact parent
BASEbf5df9a1c9211afe4dbd7267c4423e9ae575f7fa. Publish this directory's support
files under .github/qualification in a sole child of SOURCE, then use the existing
contents-read generic dispatcher on AMD64. Root reviews before publication/launch.
No Actions artifact download, additional credentials, secret, remote execution,
new cache/tool home, custom output base, VM, or permission expansion is required.

Rebuild both complete opaque //:release filesets through canonical Make/Bazel9.3.1
with matching --config=x86_64 --nostamp --jobs=3 and existing native toolchain/cache.
Before any project build, conditionally apply the prior successful native plans’
AppArmor user-namespace setting on this disposable runner with a30s deadline;
record its original and active values, and require active0 when the control exists.
Retain both complete release archives, every ELF's section identities/layout,
.text/.rodata/Go metadata bytes and host binutils reports. The included expected
inventories come from verified artifact14385941079 of successful run44208843653,
planb493429bb006e61e3fac41cd9bcf19f1ab071654; archive SHA256 was
f5ea7a68a49d03edcf804342a2faea816a94127d11225718481671b643008abd.
Compare only these actual full-release inventories; there is no obsolete separate
stub-inventory dependency. Exact full ELF hashes also bind their embedded stubs.
Require the exact full9ELF member list, lengths and hashes to reproduce for BOTH
arms before measurement. A mismatch blocks all measurements and retains new full
archives/layout/reproduction differences for root diagnosis; it is not silently
accepted as a new baseline. Full ELF retention does not recover stripped symbol
names: the prior Go .gopclntab identity and raw ELF code/data remain inspectable.

Build the unchanged BASE's existing startup_test and syscallbench_test targets
with canonical Make/Bazel; cquery names their real executables. Freeze each static
AMD64 executable and its entire materialized .runfiles tree outside the checkout.
Missing runfiles, unexpected workspace layout, extra executables, PT_INTERP or
wrong architecture fail before measurement. Record binary hashes/runfiles hashes.
The owning rule test/benchmarks/defs.bzl explicitly declares static benchmark
binaries are executed directly on the host. Makefile run_benchmark ultimately
executes these same binaries under sudo after build_paths; its benchmark-platforms
recipe installs the selected platform runtime. Those exact rule/Makefile sources
are retained in the results. The plan uses their direct-host execution path after
freezing, rather than invoking Make/build_paths again during visits. No custom
benchmark replaces the existing benchmarks; no direct Go build/test occurs.
RUNFILES_DIR and TEST_SRCDIR point at the retained tree, TEST_WORKSPACE is _main,
and TMPDIR/TEST_TMPDIR/undeclared outputs remain in task scratch/results.

Load only canonical syscallbench and alpine images while still at BASE; record
and verify both image IDs. Install pinned external benchstat commit
4febdb082b2ce2a0f9f6f496698aa5a2273279b8 with host go install outside the checkout,
using its existing canonical dependency cache and a task-temporary GOBIN; record
go version -m. This tool installation builds no project source. Shut down Bazel
before the explicit build-free boundary. No source switch or project build occurs
between the first and last timed visits; cleanup restores SOURCE afterward.

Require no preexisting Docker containers. Preserve daemon.json privately with its
original mode/owner, or record its absence. Register systrap once using the same
canonical runsc install flags (--platform systrap --profile, debug logs,
--allow-suid and TESTONLY-test-name-env), pointing at ONE actual directory
$RUNNER_TEMP/systrap-samepath/active/runsc; reload Docker. Every visit, including
A/A controls, recreates that same real directory and copies the ENTIRE selected
frozen fileset into it. No per-label executable path or symlink target is used.
Require no containers or still-running task runtime executable before replacement
and after a visit. Verify every active release member's hash, size and mode and
the entire frozen harness/runfiles before and after every visit, plus both image
IDs and the configured runtime path. Runtime log/profile paths are also constant
except the task-owned per-visit undeclared-output destination.

The fixed seeded schedule contains four cyclic rotations of base/candidate/
controlA/controlB, with randomized cycle and rotation order. Every label occurs
once in each temporal position, four visits per label; controlA and controlB both
activate the identical frozen base fileset through the same copy/verification
procedure. Each visit gives BOTH existing BenchmarkSyscallbench cases and broad
BenchmarkStartupEmpty three complete warmup samples followed by six measured
samples. All labels get equal warmups; none are truncated or reused. Four visits
give24 measured samples per label per case (getpid, getpidopt, StartupEmpty).
Use -test.cpu=4/GOMAXPROCS=4, syscall benchtime1s and startup benchtime1x.
Literal selectors BenchmarkSyscallbench and BenchmarkStartupEmpty avoid Make's
punctuation problem. Actual raw logs must contain exactly the expected names and
counts for every warmup/measurement batch, finite positive ns/op, positive
iteration counts, and no skip/failure. Retain raw logs,
schedule, host identity, all before/after verification records, strict sample
accounting and raw benchstat base/candidate and A/A output for both suites.

Phase caps: releases45+25m; archive3m/arm; ELF inspection5m/arm; reproduction1m/arm;
layout comparison1m; harness15m; cquery2m; freeze2m; harness manifest1m; images5m each;
benchstat install8m; Bazel shutdown2m; runtime register/reload1m each; sixteen
visits8m each; accounting2m; four benchstat reports1m each. Nominal cap sum265.5m including the AppArmor setup;
per-command termination grace is30s, with shorter benchmark grace10s inside each
visit's outer8m deadline. Whole script deadline300m plus up to3m termination/
restoration grace leaves27m beneath the dispatcher's330m deadline. Cleanup has
its own120s cap, removes only this task's systrap containers, restores daemon.json
bytes/mode/owner or absence, reloads Docker, verifies restoration hashes, and
restores the exact SOURCE checkout. A timeout or failed restoration fails the run;
no partial sample set can produce passing accounting.

Preserve earlier adverse measurements: run44190929074 EPYC9V45 optimizedgetpid
+3.22% p=.008 n24; run44196112563 EPYC7763 optimizedgetpid+1.20% p=.003 n24 and
ordinarygetpid+1.26% p=.032. Its identical-base A/A p=.907/.354 used the same base
path while candidate had a different path. Earlier complete benchmark ELFs were
not retained. The matched --nostamp layout run accounted for every full ELF
change, but proves no performance cause. A null same-path result does not erase
these observations; a positive result likewise needs mechanism diagnosis before
inventing a source fix.
