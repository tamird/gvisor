Diagnose the actual BASE HTTPD concurrency-1000 timeouts. Not launched.

Bind runtime to 1f33fe3003455db178b97f3b43d3a012f1611994 and frozen
canonical benchmark harness to aed925f04bead9b3d6917fbab5aac656af4b7ef8,
the exact source pair that produced run 44250351891. Fetch both exact objects
explicitly. Do not substitute the new Hey correction: retain actual raw client
output even where the canonical harness emits misleading finite metrics.
The new Hey correction has a separate unit/control qualification.

Run one representative existing leaf, BenchmarkContinuousHttpd/filesize.1Mb/
concurrency.1000, at N=1000 using the exact Bazel-built static httpd_test and full
runfiles. First native runc, then BASE systrap, at the same active opaque full
release path and same frozen harness path. No candidate runtime or statistics.
Use the actual Go selector
-test.bench=BenchmarkContinuousHttpd/filesize[.]1Mb/concurrency[.]1000\z
with argv recorded verbatim; no Make shell regex expansion. Verify exact one
final leaf and actual N=1000 plus its N=1 calibration, both raw reports retained.
Require request totals/errors/status accounting to be parseable; actual client
errors are the diagnostic outcome and must remain failed capability evidence.

Input work: two invocations, 2002 requested application responses including Go
calibration; at most 2,099,249,152 static body bytes if every request succeeds.
This excludes headers, readiness/control requests and retries; it is not a wire
byte claim. Existing matching native raw has 1000/1000 HTTP200 in 1.7007 s; BASE
Sentry has 437 HTTP200 plus 563 client timeouts in 20.0621 s. These are observations,
not a forecast or explanation of the underlying server/network cause.

Use only the three existing exact httpd/hey/util image IDs from 44250351891.
Freeze complete release fileset and tar SHA, full static harness/runfiles and
image IDs. Reuse the reviewed public 0711 /tmp root, actual host UID/GID 65534
execution probe before builds, every-release-executable access, configured
Docker true, and source-first 152 s cleanup. Work/results remain private. No
ancestor chmod, timeout tuning, TCP/backlog/resource sysctl tuning, apache configuration changes or
new runtime instrumentation.

Before workload, retain actual public-image apache2 -V and configuration dump
(apache2 -t -D DUMP_RUN_CFG), apache2.conf, ports.conf and enabled MPM config
using the same APACHE_RUN_DIR/USER/GROUP/LOG_DIR/PID_FILE values as runHttpd.
Record immutable image ID, exact command, stdout/stderr/status for every probe;
unsupported/missing tools remain unknown rather than inventing defaults. Read
host somaxconn/tcp_max_syn_backlog and CPU/memory availability without mutation.

During each canonical invocation, a bounded task-local observer records only
containers created while the previously-empty job is active, with matching
immutable httpd/hey/util image IDs. Capture actual initial inspect start times (stop times may be unavailable), container
argv/runtime/network/CPU/memory/PID limits, Docker stats, server stdout/stderr,
and readable /tmp/error.log,/tmp/access.log,/proc/1/limits,/proc/net/netstat and
/proc/net/snmp. Use bounded read-only docker exec probes; retain exact failures
if a short-lived calibration server exits before a probe. Configuration and
resource probes perturb execution, so these visits are diagnostic and cannot
be reused as performance samples. Do not infer socket/backlog/server causes
from a timeout alone. Require actual final server/client identities and raw
client accounting; if monitoring misses the relevant interval, report missing
evidence rather than tune/retry automatically.

Observed prior preparation: BASE build214.829s and all four harnesses186.334s
(~6.7min before setup); this plan builds one harness. Expected10–15min if
comparable, with cold-host/configuration-probe variation unknown. Actual phase
caps live in budget.json: exact fetch1min, public-host probe0.75min, three cached
pulls15min, util preflight0.5min, static config2min, full release15min, archive/
manifest2min, one harness10min, cquery1min, harness freeze2min, shutdown1min,
public release fileset/access1.5min, activation3.5834min, two visits/accounting
6min, and established AppArmor prerequisite0.5min:61.8334min total below67min.
The67min outer deadline plus180s cleanup grace gives70min inclusive hard max.
No dispatch or statistical campaign is authorized by this prepared plan.

The established ephemeral AppArmor user-namespace prerequisite is authorized:
record original/active values and set only apparmor_restrict_unprivileged_userns
=0 when present, before builds. Cleanup restores original. No TCP/backlog/
resource sysctl changes. Inclusive source-first cleanup is152s (20source+
12diffs+95Docker/public-root+5AppArmor+10Bazel+10chown), plus at most19s child
signal handling remains below180s grace. Failures stay failures; the worker is
then disposed. Public runtime files are removed only after actual Docker
restoration and path quiescence.

The observer retains attempted probes separately from successful nonempty
per-file/log evidence. Missing/failed files and unavailable stop times remain
explicit. Diagnostic capture completion requires actual final client/server
identity, usable logs and resource evidence, and complete request accounting
(status counts plus client error counts equal requested N) for both calibration
and final reports. Even a complete capture with request errors remains failed
capability evidence and cannot qualify the category or become a performance
sample. No automatic retry/tuning follows incomplete evidence.
