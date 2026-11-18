AMD64 NORMAL coverage performance study, approved scope; not launched

Exact SOURCE7eafb7afe2c0255c98392c2e701ec6aecad22b27, exact parent
BASE1f33fe3003455db178b97f3b43d3a012f1611994. Root approved this exact NORMAL scope and cost before publication.
Production source is unchanged; task preparation uses only existing authorized host prerequisites and tool caches.

Actual capability44214811731, pland146d4500711b502ea5b28fb2a028df08f10e33c,
artifact14390035767 passed both NORMAL coverage binaries, static installation,
symbolization/exclusions, canonical images, both existing broad benchmark
harnesses, exact Docker runtime activation, all eight leaves per source, and
source/config cleanup. Preparation1817s included551s apt package installation;
the actual benchmark commands cost18s baseline and15s candidate. These are
single observations, not significance or equivalence evidence. Earlier failed
pilots remain retained, including44211023454's immediate post-reload absence.

Expected study wall time45–60m: approximately30m measured preparation plus
50visits at approximately18s each, plus archives/hashes/tool preparation.
There are two discarded full warmups, then24 predetermined alternating AB/BA
pairs; each visit runs every one of the seven existing syscallbench leaves
at1s benchtime and existing broad StartupEmpty at1x. Every case receives24
measured observations per source. No A/A or extra variant matrix is added.
The hard whole-script deadline150m plus180s termination/restoration grace is
below the generic330m job limit. Each visit has90s outer cap, with startup15s
and syscall50s inner caps; partial schedules cannot pass accounting. The
whole-script cap overrides the sum of individual worst-case preparation caps.

Reuse the capability plan's declared native host prerequisites and verified
Go source/bootstrap archives for the old coverage action. Preserve its download
audit and both complete installed NORMAL coverage runtime filesets as archives
and full-member fingerprints. Build one unchanged BASE static benchmark harness
and materialize both entire .runfiles trees. Verify runtime and harness before
and after every visit, plus image IDs and the exact single Docker runtime path.
Every visit recreates the same actual active directory and copies the ENTIRE
selected installed runtime fileset into it. No project build, source switch or
dependency installation occurs between the first and last visits. Shut down
Bazel before that boundary. Normal release assembly and its documented two
coverage executable replacements match the successful capability plan.

Docker registration uses the proven idle-daemon restart plus bounded exact-path
configuration AND Dockerinfo readiness check. Cleanup removes only task systrap
containers, restores original daemon configuration bytes/mode/owner or absence,
restarts Docker, and restores exact SOURCE. docker_cleanup.py differs from the
reviewed same-path helper only by restart instead of reload. The other shared
fileset/freeze-harness/quiescent helpers are reused. No private daemon config is
published; only restoration hashes and task-runtime observations are retained.

Strict accounting checks all50scheduled visit exits, exact8leaf sets, positive
finite metrics/nonzero iterations, startupN=1, same host/package headers,
before/after fingerprints, and24measured samples per source/case. Raw logs, full opaque runtimes, hashes, schedule, source/plan metadata and pinned external
benchstat output are retained. Metadata fixtures use the actual successful
pilot log SHAPES, not new benchmark observations; they accept a synthetic full
schedule and reject wrongN, nonfinite, missing-leaf and unexpected-skip inputs.

Scope is AMD64 NORMAL startup and runtime costs. Race, ARM64, coverage-report
and KCOV-consumption performance are not measured or declared qualified here.
Those scopes remain separate decisions; this study does not repeat the already
verified functional KCOV or source lint checks.
