# Remote CI qualification

This is partial qualification of the remote execution environment. Run Bazel
on a BuildBuddy hosted Linux AMD64 coordinator against a published commit; its
`buildbuddy_remote_executor` configuration supplies the connection and
authentication. No custom gVisor builder image is required.

For authentication and individual remote build/test commands, see the
[remote execution guide](../../tools/bazeldefs/README.md).

See [stress testing](stress.md) to repeat selected Go, C++ or syscall tests
with the same remote configuration.

The CI-neutral entry point selects target architectures with
`--arch=amd64|arm64|all`, subject to each lane's worker requirements. AMD64 is
the default; the target selection is separate from the coordinator architecture.
For the AMD64 profile:

```sh
test/rbe/qualify.sh --header-base="$BASE_COMMIT" amd64
```

Set `BASE_COMMIT` to the explicit comparison commit for license headers and
provide complete local Git history. The dispatcher does not choose or fetch a
comparison base. The full profile also requires `COS_IMAGES_JSON`, as described
below. Use `test/rbe/qualify.sh --list` to see the lanes and
environment limits, or pass lane names to run a smaller selection, for example
`test/rbe/qualify.sh unit portforward`. It runs every selected lane and returns
failure if any lane fails, including fixture cleanup after the test cases pass.
This remote profile does not replace the full public CI matrix. It omits the KVM
variants of posture, startup, continuous benchmarks and syscall tests.
PGO also runs separately: both the remote systrap and local KVM selections
in [PGO profile generation](#pgo-profile-generation) remain required.
Public CI uses AMD64 save/restore and ARM64 save/resume;
`--arch=all` follows that mapping. Standalone checkpoint lanes use the requested
`--arch`, defaulting to AMD64. Remote ARM64 checkpoints require Firecracker
capacity; ARM64 save/resume can instead use the native Actions executor.
The root lane retains its cgroupfs owner and, on AMD64, also runs the complete
root suite under native systemd in a private PID, cgroup and mount namespace.
The test, Docker and runsc share that view and a writable delegated cgroup
subtree.
Container tests use their own runtime variants; their systemd case checks
serialized mock state and does not require a host systemd manager. Remote KVM
worker capacity remains unavailable; the Actions frontend below supplies local
KVM execution. All variants remain available through their owning Bazel targets.

With `--arch=arm64 --test-execution=remote kvm`, the KVM lane selects the
existing pinring VM-release and boot-posture unit cases inside the declared
ARM64 release-candidate guest. Its QEMU declaration enables EL2; other TCG
guests keep EL2 disabled by default. The guest must provide `/dev/kvm`,
and both selected cases must execute without skips to establish coverage.
The retained guest kernel configuration, console, test XML and original
statuses distinguish missing prerequisites from completed KVM operations.
This ARM64 cohort does not select the AMD64 Docker KVM integration suite.

To exercise unit cases that need namespaces, a particular user, SMT siblings
or ARM64 KVM, run:

```sh
test/rbe/qualify.sh --arch=all --test-target=//test/rbe:unit_guest_tests test
```

The suite boots Linux guests on ordinary remote AMD64 workers and runs the
existing test executables in them. It supplements the public unit selection;
it does not exercise AMD64 KVM. The full ARM64 container suite is available as
`//runsc/container:container_test_arm64_el2_guest`.

SlimVM remains an optional, unqualified platform: the public syscall profiles
exclude it in [.bazelrc](../../.bazelrc), and the [benchmark adapters](../../Makefile)
explicitly skip it. Its manual test targets are outside this public CI selection;
the AMD64 runtime build still includes its implementation.

This document describes lane selection and environment requirements. Execution
results apply to the recorded source, selected tests and actual workers.
Declaring a lane does not establish that its tests pass, and failures remain
part of the qualification result.

For explicit `build` or `test` lanes, pass additional Bazel options through
`actions.sh --bazel-option=--config=plugin-tldk` (or directly to `qualify.sh`).
Repeat `--bazel-option` for multiple options, using `--FLAG=VALUE` for values.
These arguments retain command-line precedence and literal characters such as
`#`; hosted runner `extra_bazel_args` are instead interpreted as rc-file entries.

For remote `--arch=all test`, select existing architecture frontends or a suite
that contains them. The combined graph retains those declared platforms;
it does not duplicate arbitrary targets for both CPUs. For example, select
`//test/rbe:platform_matrix` to run its two native owners on each CPU. Additional
explicit targets, such as the workflow checks, join the same verified graph.
Configured expected owners and actual execution records establish which
architectures ran.

## Running the supported public matrix

The `public-cpu` profile dispatches the complete supported public CPU matrix
from one source revision. It uses the same lane selectors and Actions execution
path as an individual selection. Compatible tests share Bazel platform graphs;
native ARM Docker and AMD KVM work uses separate matching Actions hosts.
The `amd64` shorthand above remains a partial remote profile.

Choose a published `qualification/` branch containing this workflow and record
its full commit. The fork needs its existing `BUILDBUDDY_API_KEY` secret. Set
`BASE_COMMIT` to the full license-header comparison SHA and obtain the complete
COS catalog as [described below](#selecting-qualification-lanes).

```sh
QUALIFICATION_REF=qualification/final-public-ci
SOURCE_COMMIT=$(gh api "repos/tamird/gvisor/git/ref/heads/$QUALIFICATION_REF" \
  --jq .object.sha)
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" -f profile=public-cpu \
  -f "header_base=$BASE_COMMIT" \
  -f "cos_catalog_gzip_base64=$(gzip -n -c "$COS_IMAGES_JSON" | base64 | tr -d '\n')" \
  -f "cos_catalog_sha256=$(sha256sum "$COS_IMAGES_JSON" | cut -d ' ' -f 1)"
```

Keep the ref fixed until dispatch is accepted. The workflow checks out its
immutable triggering SHA; the collector rejects a different source. The plan
binds that SHA, run and attempt, header base, catalog digest and clock source.
It includes both CPU build/release profiles, source checks, native and remote
unit/runtime/syscall routes, both PGO modes and source-image cohorts. PGO owners
come from the existing manifest; other owners come from their existing lanes.
The full profile rejects target, case and syscall-bucket narrowing.

The source-owned plan in `unit_matrix.py` determines the expected rows, runner
capabilities and budgets for both full and single dispatches. Each row retains
its exact configured owners and attempts; sums across rows are not counts of
unique targets. Native PHP checks supplement the full remote language lanes.
The all/local RC row retains both AMD KVM and ARM TCG guest profiles. The
`container-v1-pilot` is an optional fork guest bring-up diagnostic; public
`container-tests` does not select it, and the full profile claims no pilot run.

The qualification workflow on this fork is dispatch-only. Publishing or
updating a qualification ref does not start a build. For a single selection
the native row downloads and verifies its own uploaded result. A common
remote selection runs in one Actions job. Local ARM/KVM selections still
use their planned runner; public CPU runs still authenticate every planned
row across runners. A missing row, wrong source/attempt/digest, transfer
failure or unexpected native result fails the workflow. The combined
receipt retains FAIL when a complete multi-row result merely confirms the
native job's failure.

Start new qualification refs from `qualification/final-public-ci`. For
existing refs using this harness, carry the workflow, both
`.github/actions/qualification-*` actions and the corresponding reporter
and test changes together. Dispatch the same `build.yml` command below.
Keep test-specific expected-negative predicates in the owning study; a
nonzero Bazel exit or an unrelated failed case is not such a predicate.

To run one selection, keep `profile=single` (the default) and pass the existing
lane inputs. For example:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" -f profile=single \
  -f lanes=workflows -f architecture=all -f execution=remote
```

Record the accepted workflow run ID and attempt and attach the maintained
collector once. Full profiles publish the `all` artifact; a single selection
uses its requested architecture.

```sh
python3 test/rbe/collect_actions.py --run "$run_id" --run-attempt "$run_attempt" \
  --source "$SOURCE_COMMIT" --architecture all --watch-seconds 21600 \
  --prefix "$receipt_prefix"
```

All rows use Actions coordinators, including the three former hosted-only
routes: AMD Docker/license checking and ARM KVM guests. Those routes retain a
45-minute work budget with time reserved for reporting; long remote rows retain
120 minutes. Local benchmarks retain 250 minutes of work, a 275-minute step and
285-minute job. Original action/test deadlines, shards and attempts are
unchanged. Missing, cancelled or failed rows cannot produce a complete pass.

An always-running composition job checks the exact planned row set. Pinned
workflow-scoped artifact transfer verifies service digests; canonical receipts
bind the received tar bytes and each row's source/plan metadata. The final
bundle retains original native exits, failures, incomplete results and skips.
Source/archive/load audits are separate from canonical test verdicts and use
source-declared obligations, never successful observed loads as the expected
inventory. Missing or unsupported required audit evidence keeps the matrix
incomplete. A green test alone supplies no source-output or device-runtime
credit. The maintained `source-images` command remains available for bounded
audits of retained results.

Physical GPU/TPU execution requires the dedicated hosts described below. Missing
NVIDIA source tags leave their ABI comparisons unqualified until those sources
exist. These are separate from reported public filters and supported-scope
exclusions: SlimVM, Java's `-tl:200` policy, checked-in runtime exclusions,
kernel-dependent skips and Make's `NON_TEST_IMAGES`. Preserve those exclusions
rather than labeling the CPU matrix a global pass. Externally supplied staged
binaries are outside scope. License fetching and publishing/deploying releases
or the website are separate operations, not qualification prerequisites.

## Qualification reports

`actions.sh` captures reports for both hosted Bazel and Actions coordinators.
Before executing a test command, it resolves the same selection with
`--nobuild`. The final `qualification.tar.gz` contains a `verdict.json` with
the source commit, expected configured targets, executed attempts, failures,
skips and missing results. It also retains the original test logs and XML,
their hashes, and sanitized build events. Test failures remain failures when
report collection or cleanup also fails; missing results cannot produce a
passing verdict.

To require a particular regression to run, set `QUALIFICATION_REQUIRED_CASES`
to a JSON file in the explicit `test` lane. Map each target label to its
required XML case names, such as `{"//pkg:tests": ["Suite/Case"]}` (omit
`Suite/` if the XML has no classname). Each case must pass on its own
target in every run. It may run on any shard. Skips and unrelated passes
do not satisfy the requirement; other cases retain their usual results.

For Go tests, the XML suite name comes from the last component of the
configured test's import path. For example, `iptables_test_arm64` emits
`iptables_test_arm64/TestMasqueradePortRange`, while `ptrace_test_arm64`
embeds the `ptrace` package and emits `ptrace/TestStubFilterPrecompiled`.
Set `QUALIFICATION_GO_REQUIRED_CASES` to a JSON file mapping public target
labels to Go test names (including any subtests), for example:

```json
{"//pkg/aio:aio_test_arm64": ["TestSerialGoQueue", "TestSerialGoQueue/Read"]}
```

In the explicit `test` lane, `actions.sh` resolves these names with
`go_required_cases.py` using the actual test invocation's options, startup RC
and coordinator execution policy. It retains the input, configured generator
joins, query options, resolved XML manifest and authoring exit status. Analysis
errors stop before the native test. Their source-bound preparation failure
retains the helper exit, diagnostics and requested labels separately from the
unresolved configured owners and unstarted native tests. Missing or skipped
cases fail the ordinary required-case verdict. Use either this input or
`QUALIFICATION_REQUIRED_CASES`, not both. The helper remains available directly for other callers that already
have the exact configured Bazel options.

Build targets can tag small report groups with
`qualification-output-group=<group>`. The coordinator asks Bazel to materialize
those outputs, retains their configured owner and output-group identities, and
checks every copy against its BEP digest and size. Missing or corrupt reports
prevent a passing verdict. CodeQL declares its SARIF files and Go source summary
this way; its database directories are not report outputs.

The same bundle records `bazel run` and `bazel query` commands, including the
Gazelle checks, in a separate `commands` list. Each row retains exact arguments,
source identity, exit status, completion time and hashes of both output streams.
These commands contribute no test cases or build-aspect counts. Silent success
is valid; missing output or exit evidence cannot pass.

Collection and reconciliation run on the coordinator. Both coordinator types
publish the small verdict separately from the evidence archive, compressed for
transfer. Read the verdict for the result; download the bundle to inspect a
failure or audit the verifier. Execution completion and verdict availability have
separate timestamps. Interrupted report collection can be resumed with
`qualification_report.py capture` using the same inputs;
copies matching the previous manifest are reused. The report tool does not
rerun tests.

Authenticate a downloaded Actions ZIP or native Remote Bazel tar.gz bundle
using its digest from the GitHub or BuildBuddy API and the source and optional
exact target labels from the submission:

```sh
python3 test/rbe/qualification_report.py receipt \
  --artifact "$artifact" --sha256 "$artifact_digest" \
  --expected-source "$source_commit" --target //pkg/sync:sync_test \
  --output "$receipt_json"
```

This reads `qualification/verdict.json` from an Actions ZIP or `verdict.json`
from a native bundle; it does not extract files or reconcile reports again.
The receipt retains the producer's status, failures, skips and coverage gaps,
including zero-case and all-skipped owners. Its `consumer_verified_at` is separate from
the producer's `verdict_available_at` and each invocation's original
`execution_finished_at` and `bep_finish` timestamps. A verified FAIL or
INCOMPLETE result writes a receipt and exits nonzero; an identity mismatch
rejects the artifact.

For image consumers and image-building tests, `source-images` audits one
invocation from that authenticated bundle. Supply the retained compact execution
log, that invocation's verdict and capture manifest, and an existing BuildBuddy
CLI:

```sh
python3 test/rbe/qualification_report.py source-images \
  --execution-log "$invocation/execution.binpb" --decoder "$bb_binary" \
  --verdict "$invocation/verdict.json" \
  --manifest "$capture_manifest" \
  --expectations "$source_image_expectations" --output "$image_audit"
```

`source-image-expectations` derives the expectations JSON from the retained
`source_image_contracts` output group. It binds every configured owner,
including owners with no image dependencies, to its declared archives and
image-building commands. Registry dependencies also retain their manifest
pins and selected layout indexes. These obligations come from the build graph,
before inspecting successful loads. An optional `benchmarks` object uses the
existing benchmark expectations format (`name`, `count`, `units`, optional
`iterations`) to require specific positive measured cases. The audit reuses the
reporter's benchmark parser, including its rejection of partial output from a
failed package.

The audit follows compact input-set and runfiles edges through each image's
declared producer and archive conversion. Source-image builds must execute;
native OCI assembly and registry-copy actions can use their recorded caches.
A pinned registry dependency proves which archive was used, not that its
contents were compiled from source in this invocation.

Docker consumers record the actual archive load. CRI consumers record the
selected containerd configuration and successful imports; archives staged for
optional use remain distinct from required imports. Each observation must
match the configured test's inputs. Local TestRunner actions may lack a REAPI
digest; their target, strategy, XML output and input-set identity must still
match.

Image-building tests retain their complete selection and per-image build,
load and inspect results. The audit binds those bytes to the TestRunner's
output digests and its declared source-context archive, then checks that all
shards cover the selected cohort exactly once. An archive loaded from that
context must trace back to its declared producer and match the recorded
digest. Building the test executable alone cannot satisfy these obligations.

Audit status is separate from the unchanged canonical verdict. Missing evidence
cannot pass, and an original workload failure retains its status and native
exit even when provenance is complete. Audit every applicable test invocation;
retain auxiliary command phases and the bundle receipt when composing results.
This command neither collects artifacts nor submits workloads. Source and
archive identity do not establish reproducibility of mutable recipe inputs or
execution on a physical GPU.

For an already submitted Actions workflow, use the same receipt verifier through
the existing `gh run watch` and artifact-download path:

```sh
python3 test/rbe/collect_actions.py --run "$run_id" --source "$source_commit" \
  --run-attempt "$run_attempt" --architecture amd64 --prefix "$receipt_prefix"
```

The collector binds the requested positive attempt, or the first observed
attempt when `--run-attempt` is omitted, and rejects a change while waiting.
It selects only `qualification-<architecture>-attempt-<attempt>` and records
the artifact ID, name and digest. Legacy unsuffixed artifacts are accepted only
for attempt 1; reruns of old workflows require separate producer evidence and
are rejected by this collector.

The receipt retains requested and observed source identities and the workflow
conclusion. Missing or invalid artifacts still produce a receipt with
`collection_status=INCOMPLETE`; a known workflow failure remains `FAIL`.
Collection failures do not invent test counts or claim artifact verification.
This command never submits or retries work.

For an already submitted native BuildBuddy invocation, attach collection
immediately and leave this bounded command running through receipt creation:

```sh
python3 test/rbe/qualification_report.py native-receipt \
  --invocation-id "$invocation_id" --api-key-file "$api_key_file" \
  --expected-source "$source_commit" --target //pkg/sync:sync_test \
  --artifact "$artifact" --output "$receipt_json" --timeout-seconds 7200
```

This polls only that invocation, checks its source and single
`qualification.tar.gz` artifact, downloads the blob from BuildBuddy, and checks
the advertised SHA256 and size before using the same receipt verifier. It never
submits, retries, or cancels remote work. The key is read from the named file and
sent only to the fixed HTTPS API endpoint; redirects are rejected. The timeout
covers polling and collection, with individual network operations capped at
30 seconds. A complete matching local blob is reused when attaching again.
An interrupted download stays at `$artifact.partial` and is restarted on the
next attachment; it is never treated as a verified artifact.

The receipt includes the native invocation's exit description and timestamps.
A collection error writes an error receipt without claiming verification;
a known failed execution remains FAIL even when its bundle is unavailable.
Producer failures, incomplete coverage, zero-case owners and skips remain in
verified receipts. Native coordinator failure also fails the overall receipt,
while `producer_status` retains the bundle's original verdict.

Benchmark-only Go runs use an explicit opt-in, rather than named-test coverage.
For `actions.sh` with the explicit `test` lane, set
`QUALIFICATION_BENCHMARK_MANIFEST` to a JSON file and select the exact owner
labels with `QUALIFICATION_TARGETS`. For example:

```json
{
  "arguments": ["-test.run=^$", "-test.bench=^BenchmarkGateClose$",
                "-test.v=false", "-test.benchtime=1x", "-test.benchmem",
                "-test.count=1", "-test.cpu=1"],
  "owners": {
    "//pkg/sync:sync_test": [
      {"name": "BenchmarkGateClose", "count": 1, "iterations": 1,
       "units": ["ns/op", "B/op", "allocs/op"]}
    ]
  }
}
```

The manifest is validated and retained before Bazel starts. Its quoted arguments
apply identically to independent selection and execution. Every selected owner
must have expectations: `name` is the leaf name without Go's `-GOMAXPROCS`
suffix when using the default CPU setting. With explicit `-test.cpu`, it is the
exact output name, including any CPU suffix. Raw observed names are retained;
row counts still reject unexpected or duplicate CPU variants. `count` is the
number of rows per attempt; `units` is the exact set of metrics. Optional `iterations` checks fixed-iteration runs. Numeric values are
retained without a performance comparison or significance claim.

Benchmark mode requires successful native/BEP outcomes, unchanged source,
complete attempts and matching original output with a terminal Go `PASS`
before any appended spawn stderr. XML errors remain failures.
`-test.v=false` overrides rules_go's injected test verbosity:
benchmark RUN markers otherwise become unfinished XML testcases. An empty XML
suite is expected here, and benchmark observations are reported separately.
Ordinary test mode requires executed named cases. Generated ABI tests carry a
generator-owned applicability record: groups with no non-dynamic types, or whose
source constraints exclude this configuration, remain in `zero_case_owners` and
are reported separately as `non_runtime_owners`. They add no passing cases. Every
tagged attempt must retain one consistent, typed record and pass natively;
missing eligible tests, all-skipped tests, and unexpected empty owners remain
incomplete.

## GitHub Actions frontend

Choose a published `qualification/<name>` branch on `tamird/gvisor` that
contains this workflow, and set `QUALIFICATION_REF` to that branch before
running the examples below. The workflow accepts the `qualification/` prefix
for manual dispatch; pushes to these branches do not start qualification.
The job checks out the immutable triggering commit (`github.sha`) and uses
that same SHA for hosted execution and report source checks. Moving the branch
after dispatch does not change the source of that run.

Compilation uses BuildBuddy RBE; the `execution` input chooses remote or
local tests as described below.

The job requires the existing `BUILDBUDDY_API_KEY` repository secret. It has
read-only repository permissions and does not persist the checkout credential.
Pull requests cannot enter this credentialed job.

Each Actions row verifies the finished tarball and its separate verdict before
upload. It checks text and filenames for the literal or JSON-escaped key,
decompresses Bazel's gzip profile and Zstandard compact execution log, and
requires the two copies of the verdict to match. If a report is unreadable or
contains another archive or binary format, the public row is withheld. The
native test result and private BuildBuddy evidence keep their original status.

Pass space-separated `lanes` and an `architecture` selection; the qualification
script owns their meaning and test selection. For example:

```sh
: "${QUALIFICATION_REF:?Set QUALIFICATION_REF to a published qualification/ branch}"
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes='smoke workflows' -f architecture=all
```

Remote `architecture=all` selections use the Actions coordinator directly,
with a 120-minute work deadline, 145-minute step limit and 155-minute job limit.
Compilation and test execution stay on their declared RBE workers; each test
retains its existing deadline. The same lane and architecture validation applies,
including the explicit reports of unavailable worker requirements. For example:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes='unit release-repository syscalls' \
  -f architecture=all -f execution=remote
```

The `build` and `test` lanes accept explicit space-separated labels through
`targets`. They use the Actions coordinator for long remote operations, with
the same configured-output and test-report retention as the named lanes:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=build -f architecture=amd64 -f execution=remote \
  -f targets='//test/docker:gpu_triton_source_amd64'
```

Remote coordination allows 120 minutes of work. Individual action and test
deadlines remain separate; a qualification method must record any override.
Neither lane installs diagnostic timeout or retry policy into unrelated builds.

Explicit `test` targets also support `execution=local` with one architecture.
Select their `_amd64` or `_arm64` frontends so the existing declared root or
nonroot identity reaches the matching Actions runner. Compilation remains on
RBE, while the ordinary local-test bound and report retention apply.

The `test`, `container-v1` and `docker` lanes accept an optional
`test_filter`, forwarded as one Bazel `--test_filter` argument. Direct
callers use `--test-filter=REGEX`. Use `docker` for its native ARM Docker
fixture.
Use `container-v1` for its existing root coordinator and KVM guest; the explicit
test lane does not grant that guest device access. Record the selected cases
when reporting a filtered result. Other named profiles reject this option,
and omitting it preserves the complete lane selection.

The local `syscalls` and `syscalls-kvm` lanes and the `syscalls-rc` lane also
accept `targets` for a single architecture. Supply exact owner labels from
that lane's selection, including its architecture or guest suffix. Direct
callers use repeated `--test-target=LABEL` options. The shared selector checks
the complete configured profile and rejects unknown, excluded or wrong-CPU
owners before execution. The report retains the selected owners and every
eligible owner left unexecuted. Do not combine explicit owners with buckets.
This restriction preserves the owning lane's device setup, placement, shards
and deadlines; it does not grant the generic `test` lane new privileges.

For `license-headers`, supply the full comparison SHA as `header_base`. The
remote coordinator fetches complete history for that commit and the tested
commit before qualification. For `cos-metadata` or the full `amd64` profile,
pass the complete gcloud catalog described below as gzip/base64 data with its
uncompressed SHA256. Compression reduces the payload; all dispatch inputs must
fit GitHub's [65,535-character limit](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatchinputs).
For a captured catalog at `$COS_IMAGES_JSON`:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes='cos-metadata workflows' -f architecture=all \
  -f "cos_catalog_gzip_base64=$(gzip -n -c "$COS_IMAGES_JSON" | base64 | tr -d '\n')" \
  -f "cos_catalog_sha256=$(sha256sum "$COS_IMAGES_JSON" | cut -d ' ' -f 1)"
```

The coordinator decodes the catalog, verifies its digest and removes its
temporary copy after the run. No catalog hosting or GCP credentials are needed
on the Actions or Remote Bazel workers. Preserve the catalog's capture time
with the result; these inputs contain public metadata, not credentials.

Ordinary remote selections retain a 45-minute work limit with automatic
retries disabled. Actions reserves 25 minutes for capture/audit/bundling within
a 70-minute step and another ten minutes within the 80-minute job. Missing
workers, input errors and failed tests remain failures.

The ARM64 guest profiles include tests with one-hour deadlines, which can
exhaust the hosted coordinator's observed one-hour limit. For either
`lanes=syscalls-64k` or `lanes=syscalls-rc`, with `architecture=arm64` and
`execution=remote`, the workflow coordinates Bazel directly on the standard
AMD64 Actions runner. Compilation and every guest TestRunner remain on RBE;
the Actions host does not need the guest kernel, KVM, or local-test host
configuration.
The existing selector runs the full profile without cached test results or
retries, using 400 remote jobs. It uses a 120-minute shared work limit, a
145-minute execution/capture step and a 155-minute job; each test's declared
deadline is unchanged. Missing results after an outer deadline are
incomplete qualification, not passes. The workflow retains the configured
selection, compact execution log and sanitized test events. Remote outputs
remain digest-addressed for finite collection; payload archives need not be
downloaded to the coordinator.

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=syscalls-64k -f architecture=arm64 -f execution=remote
```

Use `lanes=syscalls-rc` for the declared ARM64 release-candidate kernel profile.

For local tests, select `execution=local`, one lane (`smoke`, `bwrap`,
`unit`, `syscalls`, `syscalls-resume`, `syscalls-kvm`, `kvm`, `startup`, `posture`,
`portforward`, `root`, `plugin-network`, `nftables`,
`docker`, `cpu-images`, `gpu-images` or `benchmarks`) and a single architecture
(`amd64` or `arm64`). The unit lane also accepts `architecture=all`: an ARM64
coordinator runs namespace-dependent ARM64 tests locally, while the complete
AMD64 profile and ordinary ARM64 tests run on RBE in the same Bazel invocation.
The original unit roots, exclusions and build-only work remain selected.
Ordinary syscalls support both architectures. Local unit,
save-resume syscall, Docker and image profiles require `arm64`; KVM package,
integration and syscall tests,
startup, posture, portforward, root, plugin-network, nftables and benchmarks
require `amd64`.
The architecture-specific test runs on `ubuntu-24.04` or `ubuntu-24.04-arm`;
Bazel compilation still uses BuildBuddy RBE with no local fallback. The
repository selects Bazel's version
through the runner's installed Bazelisk. The connection uses the same secret
in a temporary configuration that is removed after the command.

For local `bwrap` tests, use `--run_under=//test/rbe:local_root`. The test
runs as root, but Bazel runs as the Actions user. If a Go test writes its XML
report as root, Bazel cannot change its permissions and reports it as missing
even when every test passed. The helper returns an existing report to the
Bazel user without creating one and preserves a failing test's exit status
even if cleanup also fails.

The `unit`, `syscalls`, `syscalls-resume` and `syscalls-kvm` phases intersect
graph-declared architecture variants with their canonical profiles and run one
test invocation. The caller's missing-requirements policy decides which tests
on the selected local architecture need Actions. Other owners retain remote
execution, including Firecracker tests whose requirements the workers satisfy.
The test invocation's build events and execution log record configured tests,
shards and actual placement. Syscall selection reports also retain the reason
for each local route.
The local `syscalls`, `syscalls-kvm` and `syscalls-resume` Actions lanes retain
each invocation's original test logs and XML in the qualification artifact.
Their command and source records are under `<lane>-full-profile`. Report
capture failures fail otherwise successful work without replacing a native
test or build failure.
Coverage applies to the chosen architecture and profile; it excludes other
profiles and filtered build-only targets.

The hybrid `--arch=all unit` lane performs one loading-only query to discover
declared ARM64 variants under `test/unit.targets`. It reads the canonical unit
test-tag exclusions from `.bazelrc` before adding those variants, since tests
excluded from execution can otherwise still be built. The original target
patterns remain intact, including AMD64 build-only work and exclusions. One
`bazel test` invocation then analyzes and executes the mixed graph; it does not
switch configurations through separate profile analyses first. The existing
test frontends apply the caller's local requirements. The loading report records
candidate variants; actual configured owners, shards and placement come from
that test invocation's build events and execution log.
On ARM64 and in the AMD64 KVM lane, root test frontends invoke the existing
local-root fixture, which permits
traversal to `runsc` for tests that re-exec it as `nobody` and returns output
ownership to the Bazel user before validation. It changes only directory search
permission along the resolved runtime path and ownership within that test's
output directory. Nonroot tests retain the unprivileged Actions identity.
Bazel's caller-supplied `run_under` remains outside the frontend executable.
Only these native namespace/KVM TestRunners require local execution; compiler tags
and actions remain unchanged. At most two local tests run at once on the
four-core host, while remote work keeps 400 jobs.

Unit qualification also selects `//test/rbe:unit_environment_tests_amd64` and
its ARM64 counterpart for the chosen architecture. These manual frontends
reuse the original Go executables and runfiles to exercise AF_PACKET with
CAP_NET_RAW, hard links with CAP_DAC_READ_SEARCH, and the unreadable-directory
and sandbox-network validation cases as a nonroot user. The original unit
owners retain their portable environment skips. The KVM lane separately
selects the pinring VM-release and boot-posture KVM cases. It also runs the
unchanged coretag test in a two-thread SMT guest on each architecture:
AMD64 uses local KVM, and ARM64 uses remote TCG on an AMD64 worker. These
frontends retain the original 60-second deadline and require
`CONFIG_SCHED_CORE=y` with both guest CPUs sharing a thread-sibling mask.
Ordinary coretag unit tests skip when the worker lacks active SMT; the guest
frontends provide coverage on supported configurations.
A skipped required case remains unqualified even when its owner exits
successfully.

The ordinary AMD64 syscall phase uses the public `syscalls-amd64` profile,
leaving KVM and Nogo to their dedicated lanes. Its root namespace frontends
normally invoke the Docker fixture for private PID, cgroup, mount and network
namespaces. The Actions coordinator runs Bazel as root for Docker's UID mapping.
Compilation uses RBE; only TestRunners selected by the missing-requirements
policy use the Actions host.
Docker tests retain their declared images. The fixture rejects non-root namespace identities rather than
changing them and uses the private network without the outbound bridge.

`--local-test-requirements=NAME[,NAME...]` supplies requirements unavailable on
the caller's remote workers. It maps to the Bazel string-list setting
`//tools/bazeldefs:local_test_requirements`. The default `namespace` preserves
the existing fallback for every Firecracker owner, including ARM64. A caller
with capable workers can replace it with concrete missing features, or an empty
list; KVM and native initial-cgroup fixtures retain their existing local routes.
Clearing `local_test_architecture` still requests the all-remote diagnostic path.

Syscall declarations use `native_kernel_requirements` and
`hostnet_kernel_requirements` to tag only the variants that exercise the relevant
host feature. Requirements describe the test contract, independently of Actions
or a particular provider; worker upgrades change the caller policy. Each new
requirement must cite a demonstrated feature/configuration gap or kernel fix.
Missing features can also skip test bodies while their owner passes. The native
Landlock ABI, Yama, anonymous VMA naming, MSG_COPY and versioned ABI declarations
cover such observed gaps; they preserve the assertions and existing skip checks.
A test failure alone is not a capability declaration. The selector uses actual
`no-remote-exec` requirements and preserves the complete canonical owner/shard
selection, rather than excluding unsupported owners or retrying failures locally.

On either architecture, a native owner tagged
`requires-initial-cgroup-namespace` uses the local-root fixture on the disposable
hosted VM, including when other owners use Docker. Linux rejects creation of a v1
hierarchy in a delegated cgroup namespace before checking whether its controller
is busy in v2. This route requires the coordinator to share PID 1's PID and
cgroup namespaces, and the test to preserve them while owning a private mount
namespace. The helper checks the hierarchy root, rejects a preexisting `test`
group, and records and restores the root controller set and complete cgroup2
mount options for each shard. Sudo preserves the root or unprivileged Bazel
coordinator's output ownership. Restoration failures fail the test. Selecting any
such owner serializes local tests; remote compilation still uses 400 jobs.
These owners use the VM's kernel and tools, not the remote image. All owners
retain their executable, runfiles, arguments, shards and original timeouts.

Select `lanes=syscalls`, `architecture=amd64` and `execution=local` for this
route. The same hash15 partition option is available. A bounded selection or
passing bucket qualifies only its reported owners; prior failures and other
unexecuted owners remain separate.

The ordinary ARM64 syscall phase selects the existing `syscalls-arm64` 4K-page
profile. It retains the public ptrace/systrap selection and excludes checkpoint
and KVM tests. Actions installs `iproute2` and `netcat-openbsd` for the existing
rtnetlink owners. Their versions and the complete selected owner set are saved
with the result. To bound each job, optionally select one of the syscall
macro's existing `hash15` buckets:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=syscalls -f architecture=arm64 -f execution=local \
  -f syscall_bucket=0
```

The ARM64 `syscalls-resume` lane uses the public `save_resume` tag over the
same syscall roots. The runner declares this tag for its supported systrap
variants and retains their autosave/resume arguments, shard counts and
timeouts. It uses the same native frontend, host tools and hash buckets:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=syscalls-resume -f architecture=arm64 -f execution=local \
  -f syscall_bucket=0
```

The local AMD64 `kvm` lane preserves public `make kvm-tests`: the pure
`//pkg/sentry/platform/kvm:kvm_test` package test and the complete
`//test/docker:kvm_tests` integration/image suite. It also selects the declared
coretag SMT, pinring KVM and boot-posture host fixtures. All 13 owners run in one
invocation with remote compilation and per-owner test placement. The owning
Docker variants register both `--platform=kvm` and the `-docker` runtime with
`--net-raw --allow-packet-socket-write`. Original arguments, test deadlines
and source-defined skips remain in effect.

The package runs once; the integration suite retains the public job's eight
`PARTITION`/`TOTAL_PARTITIONS` name-hash partitions. The image suite uses one
owner with eight standard Bazel shards: rules_go distributes the complete
named-test list across them, avoiding empty name-hash partitions without
removing tests. The suites retain their individual deadlines. These 17 test
attempts use the existing privileged Docker fixture and pinned Docker-tools
image. The fixture supplies private cgroup/PID/network namespaces and
disk-backed scratch; its job bridge permits the suites' image downloads. The
three host fixtures use the local-root helper in a private mount namespace,
for 20 total attempts. The host must expose readable and writable `/dev/kvm`
to root. This lane is selected explicitly rather than by the remote AMD64
aggregate. Its work/job bounds are 45/50 minutes. Zero-case and all-skipped
owners still produce an incomplete qualification verdict.

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=kvm -f architecture=amd64 -f execution=local
```

The AMD64 `syscalls-kvm` lane starts from the public `syscalls-amd64`
profile and selects its existing `runsc_kvm` owners. Nogo remains in its own
lane; other public-profile owners are reported as unexecuted. The selector
requires each chosen owner to declare an AMD64 variant and uses that frontend
with the same shard count, arguments and timeout. Its KVM tag selects the
existing local-root fixture without claiming a remote KVM worker exists.
The host must provide a readable and writable `/dev/kvm` to root; actual tests
establish whether the device supports this runtime. The same network tools
and hash buckets apply:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=syscalls-kvm -f architecture=amd64 -f execution=local \
  -f syscall_bucket=0
```

Buckets range from 0 through 14. The selector first resolves the complete
canonical profile, then records its disjoint bucket partition and unexecuted
owners. A successful bucket is partial coverage; the union of all buckets on
the same source is required to cover this profile. Different buckets have
separate workflow concurrency keys. Direct callers use
`--arch=arm64 --test-execution=local --syscall-bucket=0 syscalls` and must
provide the same Linux host tools. Replace `syscalls` with `syscalls-resume`
for the save/resume profile. For KVM, use
`--arch=amd64 --test-execution=local --syscall-bucket=0 syscalls-kvm`.
Use `syscalls` instead of `syscalls-kvm` for the ordinary AMD64 profile.
Omitting the bucket selects the full chosen profile, or its KVM subset.

Local benchmark, Docker and image jobs create one temporary Docker bridge for
outbound downloads in build workloads and package-installation fixtures. The sandbox setup identifies its own Bazel
container, joins that bridge without sharing the host network namespace, and
unmounts the host Docker socket before starting the test. Docker removes each
endpoint with its sandbox; the coordinator removes the bridge after Bazel
returns and reports cleanup failures. The Actions coordinator supplies this job-owned network through
`test/rbe/actions.sh`, using its run ID and attempt for the bridge name.

The ARM64 `cpu-images` and `gpu-images` lanes run their existing two-shard
source-image checks using the same private Docker fixture. They inspect or build
workload images; the GPU image check does not execute workloads on a GPU.
Each lane gets a 70-minute step and 75-minute job so both original one-hour
shards can finish, with at most two local tests and remote compilation unchanged.

Continuous benchmarks can exceed one Actions job when queued together. To
qualify selected owners together, pass `targets` with space-separated labels
from `tests(//test/benchmarks:continuous_tests)`:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=benchmarks -f architecture=amd64 -f execution=local \
  -f targets=//test/benchmarks/fs:bazel_test_continuous_grpc_kvm_owned
```

The dispatcher checks suite membership and records the complete suite and the
selected targets. Explicit selections have separate workflow concurrency keys.
Each keeps its original benchmark arguments and one-hour test timeout. The
coordinator allows 250 minutes of work within a 275-minute step and 285-minute
job, including time to capture and upload results. A passing invocation covers
only the selected owners. Direct callers repeat `--benchmark-target=LABEL`
with local AMD64 benchmarks and provide `RUNNER_TEMP` for the selection report.

The lifecycle Go benchmarks retain their declared 30 iterations and platform
arguments within mixed continuous suites. Their configured contract tag binds
the expected leaves in `test/benchmarks/lifecycle.json`; the reporter requires
every measurement, its metrics and package completion, while retaining empty
test XML separately. The four unit targets listed in `test/benchmarks/unit.json`
exercise each declared benchmark once on each CPU in the ordinary unit run.
Their Bazel arguments leave named tests enabled. Missing measurements, failing
tests and targets whose named tests all skip still make qualification fail.

The local ARM64 `docker` lane runs the unchanged `//test/docker:owned_tests`
suite using the same Docker sandbox described below. The existing `rbe-arm64`
configuration selects native test wrappers and host tools; compilation remains
remote. Bazel owns the suite membership, architecture compatibility and image
selection. The job bridge permits the existing apt/apk fixture installations.
This lane does not use the syscall local-root frontend or require
ARM64 Firecracker capacity.

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=docker -f architecture=arm64 -f execution=local
```

The local AMD64 `startup`, `posture`, `portforward`, `root` and `benchmarks`
phases retain their complete maintained suites, including the KVM variants.
Together with ARM64 Docker, these use Bazel's Docker strategy to give each
test a privileged container and private network namespace: separate daemon
sockets alone do not isolate Docker's bridge and firewall rules. These suites load declared image archives.
The benchmark and ARM64 Docker lanes use the temporary bridge for their
downloads. The containers use the same pinned Docker-tools image as the remote
fixtures, with declared runtime and sidecars.
The Actions host supplies the outer Docker engine, kernel and devices.

For these phases, the Bazel coordinator runs as root on the ephemeral Actions
VM because its Docker strategy maps the coordinator's UID into the container.
Compilation remains remote, and the original test owners run directly without
the local-root frontend. Bazel stages their inputs, collects outputs and removes
the containers. A declared adapter binds Bazel's temporary directory to `/tmp`
so nested Docker uses the host disk for writable layers. It moves the setup
and test processes into a cgroup leaf so the private daemon can enable
controllers for its children. It verifies that both PID and cgroup namespaces
differ from the coordinator before either change. This checks whether gVisor
can run through KVM, not just whether `/dev/kvm` exists. Posture retains all six
security configurations; portforward exercises both sandbox and host networking
with the declared Redis and nginx images. The root lane retains its ordinary
root suite and the native systemd fixture, which runs that suite with the
systemd cgroup manager. Existing cgroup-version skips remain visible. The
separate `syscalls-kvm` lane owns KVM syscall selection. Local continuous
benchmarks retain their declared KVM, systrap and runc variants, workload
filters and iteration controls. The local benchmark coordinator allows
250 minutes of work within a 275-minute step and 285-minute job, reserving
time to capture interrupted results and upload the evidence bundle. It keeps
two local test slots and the original per-owner deadlines. These are ceilings,
not expected runtimes; select explicit owner subsets when useful. Results
establish only the selected workloads; shared-host timings do not establish
comparative performance.

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=startup -f architecture=amd64 -f execution=local
```

Select `lanes=posture`, `lanes=portforward`, `lanes=root` or `lanes=benchmarks`
for those suites.
Run each dispatch after the preceding run finishes, since this branch shares
a concurrency key.

The pilot runs one uncached test attempt, keeps the original target timeout,
and limits ordinary local Actions jobs to 15 minutes. Local syscall and Docker
lanes have a 45-minute work limit within a 50-minute job; the separate benchmark
limits are described above.
Local test results are not uploaded
to the shared action cache. Its artifact contains each build/test execution
log and host facts, plus native profile selection metadata where applicable,
excluding the credential configuration and raw build-event options. The
rootless smoke requires a nonroot 4K-page Linux host with working user
namespaces. Like the Buildkite test hosts, the ephemeral Actions VM lifts
Ubuntu's AppArmor unprivileged-user-namespace restriction for local tests;
AppArmor remains enabled. Recorded device nodes do not establish usable KVM
or vhost-net. Coverage is limited to the selected suite; the pilot adds no
64K-page, alternate-kernel or GPU coverage.

This fork frontend skips the legacy builder and `runsc` artifact upload on its
qualification and local-test pilot branches. Other pushes retain that job;
release artifacts remain owned by the existing release lanes. A smoke result
does not establish full public qualification.

## PGO profile generation

The `pgo` lane runs the six benchmark sources selected by public
`benchmark-refresh-pgo`, with its verbose whole-benchmark `20s` flags and
`-pgo-benchmarks=true`. The existing benchmark wrapper and owned Docker daemon
collect profiles with the public three-second GC interval and filename pattern.
The existing Ruby workload retains its own fixed-iteration override.

Select the six `*_pgo_systrap_owned` owners with `architecture=amd64` and
`execution=remote`; select their `*_pgo_kvm_owned` counterparts with
`execution=local`. Both use remote compilation. The KVM lane reuses the Actions
Docker bridge and serial local test execution. Each action keeps the existing
eternal benchmark timeout; the coordinator has a 120-minute work deadline,
145-minute step limit and 155-minute job limit.

Set `PGO_SYSTRAP_TARGETS` and `PGO_KVM_TARGETS` to the six space-separated
owner labels for each mode in `test/benchmarks/pgo.json`, then dispatch
both selections:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=pgo -f architecture=amd64 -f execution=remote \
  -f "targets=$PGO_SYSTRAP_TARGETS"
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=pgo -f architecture=amd64 -f execution=local \
  -f "targets=$PGO_KVM_TARGETS"
```

`test/benchmarks/pgo.json` records all 23 active leaves and deliberate PGO
exclusions per platform. The producer requires every active measurement and the
exact exclusions, retains XML failures, and rejects partial or empty runs.
After containers and the daemon stop, `profiletool merge` produces a profile
with positive CPU samples. `check-similar` compares it with the committed
profile for the same Go runtime key, benchmark and platform at the public 0.7
threshold. Ordinary drift records `refresh`; it is not a generation failure.
A separate threshold-zero comparison validates both inputs before interpreting
that shared exit status. A missing matching baseline records `add`.

Each owner retains its merged profile and `pgo/result.json` in the normal
qualification bundle. The producer verifies the generated digest, profile
identity and comparison decision. The lane does not apply the 28-day/source-age
publication gates, replace repository profiles, or commit them to master.

## Linux kernel coverage

Linux serves as both the host for gVisor and the native reference for syscall
tests. The documented [Linux 5.6+ host requirement](../../README.md#requirements)
is not a claim that every syscall test passes on every kernel from 5.6 onward.
Tests for behavior that changed in Linux must account for older native kernels
without weakening the expected gVisor behavior. Version checks can conservatively
skip native tests, but cannot prove that an older kernel lacks a backported fix.

The current kernel environments are:

| Environment | Architecture | Kernel coverage |
| --- | --- | --- |
| Hosted Firecracker | AMD64 | Observed Linux 6.1.0 in the October 2, 2026 qualification runs; partial qualification with remaining failures. |
| Hosted Firecracker | ARM64 | No execution capacity verified; no guest kernel qualified. |
| Public CI ordinary syscall pools | AMD64 / ARM64 | Linux 6.8.0-1069-gcp, observed October 2, 2026. |
| Public CI release-candidate syscall pools | AMD64 / ARM64 | Linux 7.3.0-070300rc3-generic, observed October 2, 2026; these pools change over time. |

The [public pipeline](../../.buildkite/pipeline.yaml) defines these CI pools.
Public master builds [49276](https://buildkite.com/gvisor/pipeline/builds/49276)
and [49266](https://buildkite.com/gvisor/pipeline/builds/49266) report the listed
kernel releases in both architectures' job logs. AMD64 runs native reference
and gVisor tests; ARM64 selects the ptrace and systrap gVisor tests. The
release-candidate version changes as its worker pools are updated.

A pinned action image selects userspace, not the worker's kernel. Kernel
configuration and exposed devices also affect test behavior. Qualification
reports must identify the actual worker kernel release/build and available
configuration evidence, with uncached results for that environment. Passing on
one observed kernel does not qualify another kernel or a release-candidate lane.
The ARM64 TCG routes below instead declare their guest kernels as build inputs;
they do not establish binary/config equivalence with the public workers.
Provider-native kernel selection and its effect on cache identity remain
[provider requirements](https://github.com/buildbuddy-io/buildbuddy/issues/13523).

## Selecting qualification lanes

The dispatcher uses the existing Nogo and unit configurations, the declared
runtime suites, and the syscall roots shared with Make in `test/syscalls.targets`.
Public CI's ordinary syscall selections live in the `syscalls-amd64` and
`syscalls-arm64` configurations. Their loading options are shared by analysis
and test commands. The AMD64 RBE lane retains its stricter
filter because KVM workers remain unavailable and Nogo has a dedicated lane.
Bazel replaces repeated
`--test_tag_filters` values, so that restriction cannot be appended to the public
configuration. The ARM64 lane uses the public configuration directly.
Lanes with invocation-wide settings use separate Bazel invocations. The unit,
release, syscall, smoke, do, Docker, root, port forwarding, bwrap, workflow and
language checks can share one invocation. Connection settings and credentials
come from Bazel's configuration; no builder container is started. Normal Bazel
caching remains enabled.

The RBE configurations default to 400 concurrent actions. Bazel's `auto` default
follows the coordinator's CPU count
([Bazel 8.5.0](https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/buildtool/BuildRequestOptions.java#L488-L490)),
which limits remote concurrency on a small coordinator. To choose another
limit, pass `--jobs` after `--config=rbe` or `--config=rbe-arm64`.

The `workflows` lane runs the declared actionlint check and the existing GitHub
and Buildkite schema tests. Actionlint uses the same workflow inputs as the
GitHub schema check; `tools/lint.sh actions` invokes that same Bazel owner.
The separate `lint` lane runs five declared formatting and spelling tests
concurrently, including alongside other `--arch=all` lanes on AMD64 workers.
Their tools and checker-specific indexed sources are Bazel inputs;
configuration files retain their project-relative paths. `make lint-fix` uses
the same selection and formatter implementation with host-native executables.
The `lint-cc` lane builds `--config=lint-cc`, shared with `make lint-cc`.
The config owns the recursive `//test/...` and `//tools/...` roots, clang-tidy
aspect and report outputs; each configured compile retains its actual arguments
and declared inputs. It can accompany `--arch=all` lanes but remains a separate
AMD64 build invocation, so those recursive roots do not run unrelated tests.

The `gazelle` lane runs the public `//:gazelle_check` target once, including
when `--arch=all` is selected. It compares the allowlisted BUILD files with
Gazelle's output. The tool builds on RBE and checks the immutable checkout
on the hosted coordinator, matching the [public workflow](../../.github/workflows/gazelle.yml).

The `governance` lane runs `//:governance-check`. Its remote generator consumes
the governance YAML files and a watched list of repository directory names.
The comparison tests check CODEOWNERS and MAINTAINERS.md against those outputs
without modifying the checkout. Run `bazel run //:governance-regen` to copy the
same generated outputs into the source tree. This lane can share a
mixed-platform invocation with the other tests.

The `license-check` lane calls `make license-check DOCKER_BUILD=false`
to compare the checked-in license catalog with the current dependency graph and
license policy. This lane requires the hosted AMD64 coordinator. Its scoped
Bazel configuration preserves the caller's rc files and does not introduce a
cache or output base. The `license-fetch` lane uses the same configuration and
canonical checker with `--mode=fetch`. It seeds the coordinator's
`qualification/dependencies.yaml` from the checked-in catalog to preserve
unchanged manual audits, then writes the generated catalog there using `--yaml`.
The common evidence bundle retains that output for review and later import;
source and lockfile checks remain strict. Fetch requires the coordinator's
existing evidence directory and refuses to overwrite an earlier output.

The `license-headers` lane resolves `--header-base` and HEAD to immutable commit
IDs, then checks newly added files as the declared `//tools:license_headers_test`.
It can join `--arch=all` invocations. The existing shell checker owns the header
policy and exclusions; its standalone CLI also selects this test. The repository
input rule watches current contents, including absent added paths, and uses
explicit 50% rename detection to exclude renamed files. Present inputs must
resolve within the checkout to avoid uploading outside symlink targets.
Ordinary graph queries need no comparison base; running the manual test without one fails clearly.
The `python-distributions` lane builds
`//sandboxexec/sandbox/python:dist` with the canonical metadata's version;
registry version discovery, installation and publication are separate work.
The `codeql` lane runs the workflow's Go, JavaScript, Python and Ruby analyses as
four declared Linux AMD64 Bazel actions. It pins the complete CodeQL 2.27.1
bundle, including compatible query packs and notices, and uses the same default
code-scanning suites and language categories. The default outputs are four SARIF
files with per-file coverage and extraction diagnostics. Build
`//tools/codeql:diagnostics` to also retrieve the databases and logs.
For Go source identities without the database, request `--output_groups=+codeql_summary`.
Uploading results to GitHub is separate.

JavaScript, Python and Ruby read every indexed source path using its current
contents, including files outside the Bazel build graph. Stage new paths before
analysis; missing indexed files and sources resolving outside the checkout are
errors. This input does not preserve Git modes or symlink metadata.

Go uses the declared SDK and the existing rules_go package driver over Bazel's
ordinary library, binary and proto package metadata. Generated sources, cgo
outputs, embeds and patched dependencies remain declared inputs. Tests are not
enabled, matching the public workflow; `testonly` helper libraries remain in
scope. Manual access aliases preserve each owner's visibility and configuration
without changing public build or test selection. The extractor retains the
declared package directories and root module identity instead of rediscovering
them from a copied source tree. Extraction requires exact eligible source hashes
and no package-loading or extraction errors; SARIF findings remain advisory.

Regenerate the checked-in Go roots after changing declarations, using the
canonical Bazel query and generator:

```sh
bazel query 'attr("tags", "codeql-go-owner:", //...)' --output=xml > /tmp/codeql-roots.xml
bazel run //tools/codeql:update_go_roots -- --input /tmp/codeql-roots.xml
```

Passing `--check` to the generator checks for drift. Root generation loads
declarations; it does not analyze or build their runtime data dependencies.
The Gazelle presubmit and matching qualification lane run this drift check.

Run the mixed-platform units and release repository checks together:

```sh
test/rbe/qualify.sh --arch=all unit release-repository
```

This uses one `bazel test` invocation. The unit selector preserves the AMD64
selection and adds the declared ARM64 variants, reporting unavailable ARM64
Firecracker workers. The existing release graph builds Debian, bzip2 and zstd
packages for both CPUs, then checks signing and APT metadata on AMD64. Its
owning test preserves the release build's default stripping policy even though
unit tests use `--strip=never`. Either lane can also be selected alone with
`--arch=all`; their order does not change the selection.

Add `nogo` to select the complete existing Nogo lane in the same invocation.
Each Nogo target already analyzes AMD64 and ARM64. Bazel selects its owners
under the public `nogo` configuration before combining them with the other
lanes. Unit and ordinary syscall selection then remove only their Nogo
exclusion. The complete combined test set must match the selected profiles
before execution; unit and syscall wildcard roots retain their build-only work.
The final invocation does not inherit Nogo's positive tag filter or
`--build_tests_only`.

Add `smoke smoke-race` to run the existing normal and race smoke checks in that
same invocation, or run just those two lanes with `--arch=all`. Normal smoke
declares both public architectures; the race check executes on AMD64. The race
target owns its instrumentation settings, so selecting it does not instrument
the other tests. Both use the declared release sidecars and the existing
rootless namespace setup.

The `do`, `docker`, `root`, `portforward`, `bwrap` and `workflows` lanes can also
join this invocation, or run together without unit or syscall tests:

```sh
test/rbe/qualify.sh --arch=all smoke do workflows bwrap
```

The combined selection follows the public CI architectures: normal smoke,
Docker and bwrap include ARM64; `do`, root, port forwarding and workflow
checks retain AMD64. Bazel expands each architecture's owning suites, including
explicit manual tests, before the selector checks their declared variants and
configured worker requirements. Unavailable ARM64 Firecracker tests are
reported and omitted from execution. Declaring and analyzing these variants
does not establish ARM64 runtime coverage.

The selected tests must survive the final invocation's filters; a changed
selection fails before execution. Single-architecture lanes and combined lanes
use the same owning suites. Per-test runtime inputs, privileges and sharding
stay with the owning rules.

The `unit-v1`, `container`, `container-v1` and `docker-v1` lanes also join
`--arch=all` invocations. Their cgroup profiles execute on AMD64. The v1
variants own the existing mount wrapper, `CGROUPV2=false`, root privileges,
disposable worker and no-local execution policy. They reuse the original test
inputs, arguments, environment, coverage, runfiles and shard/timeout metadata;
unit selection includes its non-Go/C++ owners. The explicit container-v2
variant owns `CGROUPV2=true`, while the ordinary target still accepts local
Make overrides. Container selection expands the aggregate test into its declared
systrap and KVM frontends, then reports and excludes unavailable KVM execution.
Both frontends share the original compiled test and runfiles, preserve its eight
shards and timeout, and run all test cases with the selected platform. The suite
already excludes ptrace. Cases that do not parameterize their configuration run
on the default systrap platform in either frontend. The original aggregate test
and Make's full KVM-containing selection remain unchanged.

```sh
test/rbe/qualify.sh --arch=all unit unit-v1 docker docker-v1
```

When one of these lanes is present, Bazel first selects the ordinary unit tests
under the public unit filters, then combines explicit ordinary and v1 leaves.
This prevents unit filters from suppressing selected container tests.
A separate canonical analysis retains unit non-test build roots. Filtered
tests' build-only work is not retained by this combined mode: standalone
`unit`/`unit-v1` preserve the complete original selection, and `build-all`
remains the all-target compilation lane. Final canonical analysis rejects a
caller filter that drops any explicitly selected test.

The test frontend is the existing with_cfg implementation, extended with an
incoming cgroup transition. The pinned dependency patch exposes that frontend
for reuse because its released API transitions only the inner target. This
keeps argument expansion, provider forwarding and executable layout in the
same owner rather than maintaining another wrapper.

The same path also accepts `overlay`, `swgso`, `hostnet`, `containerd`,
`fsstress`, `packetimpact`, `iptables`, `nftables`, `packetdrill`, `kubernetes`,
`podman`, `syzkaller`, `go-export`, `cpu-images` and `gpu-images`. These lanes
retain their existing suites and target-owned runtime settings. Combined
selection uses AMD64; standalone ARM64 selection remains available where the
existing lane supports it, with ARM64 Firecracker capacity still unqualified.
The image checks retain four AMD64 shards or two standalone ARM64 shards.
They check image availability/builds, not GPU runtime execution. Adding a
lane to the combined graph does not resolve its existing workload failures.

```sh
test/rbe/qualify.sh --arch=all smoke packetdrill workflows
```

The `cos-metadata` lane checks COS image driver metadata against nvproxy's
supported drivers. It needs HTTPS access to the public COS driver metadata,
but no GPU, COS worker, installed runtime or GCP credentials on the test worker.
Supply the complete output of the public CI query from a coordinator with
authorized GCP access:

```sh
gcloud compute images list --project cos-cloud \
  --filter="family:cos*" --format json > /path/to/cos-images.json
COS_IMAGES_JSON=/path/to/cos-images.json \
  test/rbe/qualify.sh --arch=all cos-metadata smoke
```

Preserve that query's full catalog and record its capture time when reporting
results. The dispatcher does not authenticate, query GCP or replace a missing
catalog. An absent or unreadable file fails before any selected lane runs.
It copies the catalog into the temporary repository input
`test/gpu/cos_metadata_input/images.json`, which the existing test declares as
runfile data. The helper refuses a pre-existing input directory and removes
only the directory it created after the selected lanes finish. Do not commit
this generated input. Building without a catalog remains supported; explicitly
running the metadata test without one fails with an input error.

Combined selection runs this owner on AMD64. The test retains the catalog's
existing image/version checks and live driver metadata queries, including the
existing treatment of not-yet-published metadata. Its `external` tag disables
cached test results because the driver metadata can change independently of
the catalog. The catalog is a declared input, not a frozen snapshot of those
live responses. The public `tools/gpu/cos_drivers_test.sh` caller obtains the
same catalog and uses the same declared-input helper and Bazel test.

The `posture`, `startup` and `benchmarks` lanes can join the same invocation on
AMD64. Bazel selects their runtime owners under the existing `-requires-kvm`
filter before combining them with other lanes. The final analysis checks that
unit, syscall and build-scope caller filters retain every selected owner; it
does not read test-command-only rc entries. The KVM filter is never applied to
unrelated lanes. A loading-only query reports the excluded KVM test identities
from the same owning suites, without claiming their configurations or execution.

```sh
test/rbe/qualify.sh --arch=all posture startup workflows
```

These combined additions preserve runtime selection, not the filtered KVM tests'
build-only work. Standalone invocations retain their original suite roots and
filters, including that build coverage. Benchmark arguments, durations and
resources remain target-owned; combining the lane does not shorten workloads or
resolve existing failures.

The `release-artifacts`, `python-distributions` and `website` build lanes can
join the same invocation. Release packaging builds both AMD64 and ARM64 through
the same artifact group used by the repository test; website qualification stays
on AMD64. Release and website targets preserve the public stripping policy when
selected beside runtime tests. A selection containing only build lanes invokes
`bazel build`; adding a runtime lane uses one `bazel test` invocation for both
builds and tests.

```sh
test/rbe/qualify.sh --arch=all release-artifacts python-distributions website smoke
```

The `build-all` wildcard lane remains separate: adding `//...` to a test
invocation would run unrelated tests as well as building their executables.

Select ARM64 targets with `--arch=arm64`, for example:

```sh
test/rbe/qualify.sh --arch=arm64 unit
```

The ordinary `syscalls` lane shares the public ARM64 ptrace/systrap selection
through `--config=syscalls-arm64`, excluding native and save/restore tests.
Test lanes use `rbe-arm64` to select ARM64 build tools and execution workers;
`aarch64` selects the target architecture. Build lanes use AMD64 host tools and prefer
AMD64 execution workers for either target architecture, retaining declared
native generator requirements. Selecting an architecture does not establish
worker support or qualify the other lanes.

The AMD64 `do` lane runs Make's three `do true` smoke checks against the
declared release: rootless with default networking, rootless with no network,
and privileged with default networking. The rootless cases start as an
unprivileged user; all three require the declared sidecars and retain normal
sandbox isolation. The privileged case's worker image supplies iproute2 and
iptables for its network setup. No Docker daemon or installed runtime is used.

The presubmit build lane retains the public pipeline's two build commands:

```sh
test/rbe/qualify.sh --arch=all presubmit-build smoke
```

For each selected CPU, it builds `//pkg/...` with the default `-nogo` build
filter, then `//runsc/...` with `--build_tag_filters=-network_plugins` replacing
that filter. `--arch=amd64` and `--arch=arm64` select one CPU; `--arch=all`
builds both. Builds use AMD64 host tools and prefer AMD64 execution workers,
retaining declared native generator requirements. They do not require ARM64
Firecracker workers.

Each root keeps its own Bazel build invocation and default compilation/stripping
settings. These wildcard builds do not join the combined test invocation, which
would execute unrelated tests. A failed build remains a failure while later
roots, architectures and requested lanes continue in the same hosted job.

The continuous all-target build retains the public pipeline's selection:

```sh
test/rbe/qualify.sh build-all
```

It builds `//...` with `--build_tag_filters=-network_plugins`, preserving the
default compilation and stripping settings. Bazel's wildcard selection omits
manual and incompatible targets; this lane builds test executables but does
not run them. The separate runtime lanes remain necessary.

With `lanes=build-all`, `architecture=amd64` and `execution=remote`, the Actions
frontend uses the remote coordinator with a 120-minute work limit, a
145-minute execution/capture step and a 155-minute job. Leave `targets` empty;
the lane owns the recursive selection above. Compilation stays on RBE with
400 remote jobs and no local fallback. The coordinator retains the checked
lock and source-integrity evidence through its existing all-exit capture.

The separate AMD64 plugin build retains `make runsc-plugin-stack`'s optimized
TLDK configuration, which the all-target build excludes:

```sh
test/rbe/qualify.sh plugin-build
```

The `//runsc:runsc-plugin-stack-build` target selects the existing plugin
binary with `compilation_mode=opt` and `strip=sometimes`. Its configuration
does not affect ordinary targets in the same invocation.
The separate AMD64 runtime lane preserves `make plugin-network-tests`:

```sh
test/rbe/qualify.sh plugin-network
```

It uses the plugin runtime and sentry sidecar with `--network=plugin`, and
runs `ConnectToSelf` in its owning integration package. As in Make, this
lane leaves the runtime platform at its default; its public agent's KVM
capability requirement does not set `--platform=kvm`.

For the same suite on an AMD64 Actions host, dispatch `plugin-network` with
`execution=local`. The coordinator requires readable/writable `/dev/vhost-net`
and `/dev/net/tun`; their presence alone does not establish plugin support.
The existing privileged Docker fixture gives each test a private daemon,
network namespace and disk-backed scratch, while compilation stays on RBE.
The declared images are preloaded and `ConnectToSelf` needs no outbound network,
so this lane does not attach the job bridge. The integration owner retains its
original 900-second timeout and filter. The local job retains two test slots
and a 45-minute Actions step cap within the 50-minute job.

Both plugin lanes also accept `--arch=all` alongside other lanes. They retain
AMD64 targets; this adds no ARM64 plugin or vhost-net worker capability. The
network tests own the TLDK/cgo configuration and retain their normal compilation
mode and `strip=never`, including the runtime and all release sidecars. The
build-only plugin root is built by the same final Bazel invocation.

`--config=plugin-tldk` remains available for direct Bazel and Make callers. It
sets the same default-off `//external/tools/plugin-stack:tldk` flag used by the
configured owners, replacing the old `plugin_tldk`/`network_plugins` defines.
The raw plugin binaries and backtrace test remain available under that config,
with their existing default selection unchanged.

The full graph was qualified with Bazel 8.5.0 on a 16 GiB hosted coordinator
using an 8 GiB JVM heap (`--host_jvm_args=-Xmx8g`). Apply this as a coordinator
Bazel startup option, before the subcommand. It sizes Bazel's server heap;
remote action resource requests remain separate. Keep this setting in the
coordinator configuration rather than the shared project rc. The default heap
exhausted during the full build; these are tested settings, and minimum memory has
not been measured.

The complete existing Nogo lane uses its normal tag-based selection:

```sh
bazel test --config=rbe --config=x86_64 --config=nogo //...
```

Each Nogo target already checks both supported architectures. This command
does not execute ARM64 runtime tests. `make nogo-tests` uses the same selection
configuration.

The complete existing unit selection is shared with `make unit-tests`:

```sh
bazel test --config=rbe --config=x86_64 --config=unit \
  --strip=never --sandbox_add_mount_pair=/tmp --keep_going
```

Run this command from the workspace root without additional target arguments.
`test/unit.targets` owns the package roots and four existing exclusions; the
configuration retains the Nogo, KVM and plugin filters. It also retains the
non-test targets built by the existing wildcard selection.

The public unit matrix runs on AMD64 with cgroup v1 and v2, and on ARM64. The
commands above do not qualify the complete architecture/cgroup matrix. This
lane has no separate race variant in the public pipeline.

Use `test/rbe/qualify.sh unit-v1` for the AMD64 cgroup-v1 selection. Its
disposable-VM setup is shared with the Docker-v1 lane described below.

The `container` and `container-v1` lanes retain the public AMD64
`make container-tests` selection: `//runsc/container/...` with only Nogo tests
filtered out. Unlike the unit lanes, they include the KVM cases. `container`
sets `CGROUPV2=true`; `container-v1` uses the same disposable cgroup-v1 setup
as `unit-v1`. Remote-only selection reports and excludes KVM owners because
Firecracker workers do not supply nested KVM. For the complete cgroup-v2
selection, dispatch `container` with architecture `amd64`, execution `local`
and `local_test_requirements=kvm`. Its declared architecture variants retain
all public owners, shard counts and deadlines. KVM tests use the existing
Actions Docker fixture and `/dev/kvm`; eligible non-KVM tests and compilation
remain remote. The coordinator allows 80 minutes of work plus report capture
inside the 90-minute step and 95-minute job. Only current-invocation test logs
and XML reports are retained, alongside the selected owners and spawn trace.

The `container-v1` KVM owner uses a declared AMD64 Ubuntu 6.8 guest with the
existing controller fixture inside the VM. The guest includes the required
v1 controllers, util-linux and awk. Additional kernel controllers remain
untouched; the fixture still requires every mounted controller and checks
that all controllers are initially unused and enabled. Existing non-KVM
cgroup-v1 owners retain their disposable remote worker route.

For optional guest bring-up, dispatch `container-v1-pilot` with architecture
`amd64` and execution `local`. It runs the existing KVM package test through
the same guest and fixture with its original 300-second limit. It is not a
public pipeline requirement. Dispatch `container-v1` with those same inputs
for the complete public container selection. The KVM guest has 6 GiB of
RAM and 8 GiB of scratch; only one runs locally at a time, accounting for
the four-GB container workload plus
guest overhead on the observed 16-GB Actions host. Eight original 900-second
shards have a 140-minute work budget inside a 150-minute step/155-minute
job. The probe has a separate 20-minute work bound inside its 45-minute
step. Neither a passing probe nor host `/dev/kvm` access qualifies the full
container lane. The original payload environment, arguments, shards and
individual timeouts remain part of result verification.

Fully remote ARM64 unit qualification requires ARM64 Firecracker workers for
these namespace-dependent owners:

- `//runsc/cmd:cmd_test`
- `//runsc/sandbox:sandbox_test`
- `//sandboxexec/sandbox:sandbox_test`
- `//sandboxexec/sandbox/python:sandbox_py_test`

Without those workers, select `lanes=unit`, `architecture=arm64` and
`execution=local` in the [GitHub Actions frontend](#github-actions-frontend).
That route keeps compilation and other unit tests remote while running these
owners on the Actions runner. The remote-only profile still reports unavailable
workers as errors. HTTPS tests use the existing multiarch networking image for
CA trust. Docker suites use a separate provider image that declares both Linux
architectures and supplies their daemon tools.

The `fsstress` lane runs the complete three-case filesystem stress suite
against the declared release and a private Docker daemon:

```sh
test/rbe/qualify.sh fsstress
```

It uses `//test/fsstress:fsstress_test_owned` with the source-tagged
`basic/fsstress` image pinned for each architecture. The existing gofer,
bind-mounted gofer and tmpfs cases retain their operation counts, process
counts and randomized seeds. `make fsstress-test` retains its installed-runtime
entrypoint. ARM64 execution remains unqualified.

The source-built release smoke test starts a sandbox and runs `true`:

```sh
bazel test --config=rbe --config=x86_64 //:release_smoke_test
bazel test --config=rbe --config=x86_64 --config=race //:release_smoke_test
```

Its declared release fileset selects matching runsc and Sentry instrumentation,
and strict sidecar lookup checks the installed file layout. Remote runs use the
unprivileged `nobody` identity in Firecracker to exercise rootless namespace
setup. The race variant preserves the public smoke lane's rseq setting.

The existing Make smoke targets retain their installed-binary interface,
including staged bundles and custom runtime or sidecar selections. The Bazel
target qualifies the source-built release without installing it on the
coordinator or using the builder image.

The release artifact lane builds the three packages selected by the public
release test's `make artifacts/<architecture>` steps:

```sh
test/rbe/qualify.sh --arch=amd64 release-artifacts
test/rbe/qualify.sh --arch=arm64 release-artifacts
```

Each invocation runs `bazel build` for `//debian:debian`,
`//debian:gvisor-release-tar-bz2` and `//debian:gvisor-release-tar-zstd`.
The existing package rules own the Debian metadata, compression and sidecar
layout. Both invocations use `--config=rbe` for AMD64 execution, paired with
`--config=x86_64` or `--config=aarch64` for the selected target architecture.
The lane uses `-c opt`, matching `.buildkite/release.yaml`. The mixed-platform
artifact and repository targets also select optimized dependencies, so their
configuration is independent of other tests in the invocation. Building the
ARM64 packages does not execute their binaries or qualify ARM64 runtime tests.

Building these artifacts is only part of the public release test. The release
repository lane also exercises its `make release` scripts:

```sh
test/rbe/qualify.sh release-repository
```

The AMD64 test cross-builds the same three packages for both architectures and
runs `tools/make_release.sh` and `tools/make_apt.sh` with an ephemeral test key.
Make and the test share the key-generation script. The test verifies both Debian
package signatures, signed APT metadata and raw archive checksums; only public
verification metadata is retained as test output. The packages keep the public
build's default stripping settings.

A declared OCI image supplies the release tools from an immutable Ubuntu Jammy
APT snapshot. The existing private Docker fixture owns the daemon, and native
runc executes the scripts with networking disabled. Only the declared scripts
and packages are mounted. The canonical wrapper generates master and nightly
repositories; its shared generator consumes explicit tag inventories for tagged
repositories. The test checks dated, point-release and latest archives against
the source-built inputs, including Python distributions, and verifies that an
older point release leaves latest untouched. No Git metadata or Python uploader
is present. Qualification covers outputs built from source on RBE;
externally produced staged release bundles are outside its scope. Make's
separate staged-binary support remains available.

The CPU and GPU/ML image-source lanes build the public image selections:

```sh
test/rbe/qualify.sh cpu-images gpu-images
```

They select the `tools/images.mk rebuild-cpu-images` and `rebuild-gpu-images`
owners through `//test/docker:cpu_image_sources_amd64_test` and
`//test/docker:gpu_image_sources_amd64_test`, or the corresponding `arm64_test`
targets with `--arch=arm64`. With `--arch=all`, both public profiles are analyzed
and their target-configured variants use the same worker-capacity selection as
smoke, Docker and bwrap. Each architecture runs natively, using the public
four AMD64 or two ARM64 partitions as Bazel test shards. Each shard records
its complete image selection before building, then continues independent
builds after a failure while retaining Make's nonzero exit status. Images
with Dockerfiles are built even if a matching registry image exists.
Bazel-native images load their declared source-built archive instead.
Base-image and package downloads retain their network behavior.
The GPU/ML cohort includes Make's TPU images and preserves its `NON_TEST_IMAGES`
exclusions. These checks build images; they do not run their GPU/TPU
workloads or require accelerator devices. ARM64 image checks can use
`execution=local` in the [GitHub Actions frontend](#github-actions-frontend).

The action uses declared Make and crane tools with a private native-runc Docker
daemon. It needs no gVisor release or preloaded images. Complete contexts are
materialized from the declared `//images:cpu_source_contexts` or
`//images:gpu_source_contexts` archive so Make's
`find -type f` hash sees physical source files. The archive declares regular
file modes and the tracked executable overrides; this avoids Bazel marking
every remote input executable. The current contexts contain no symlinks; new
symlinks or executable files must be reflected in that archive declaration. The
legacy default Dockerfile remains an image-test subject, not a prerequisite
builder for the coordinator.

Each shard requests the existing Docker worker image, four CPUs, 8GB memory
and 40GB disk. These are capacity allowances, not measured minimums. The
declared one-hour test timeout does not override a shorter coordinator work
budget; cancellation leaves incomplete qualification. Fully remote ARM64 image
checks require ARM64 Firecracker workers. The Actions route instead runs the
selected image-check owners on its native ARM64 runner.

Runtime suites consume Docker archives through `source_image_archive`.
It invokes the canonical Make rebuild target for the selected context and
architecture, using declared Buildx and a BuildKit container pinned by the
MODULE digest. The source action does not read a published image as a
substitute for building its Dockerfile.
Buildx uses the caller's Docker daemon, including ordinary non-root Docker
users, and removes its task-specific builder afterward. It does not require a
global BuildKit socket or a separately installed Docker CLI plugin.

The source build streams BuildKit's OCI tar output through the declared
BSD tar extractor into task-owned
scratch, then copies the completed layout to its declared output. This keeps
output writes outside the live BuildKit session, whose health checks timed out
during earlier large exports. The cause of those stalls remains unverified.
The client also runs from scratch after a prior VFS execution received SIGBUS;
direct execution remains unqualified for large builds. The builder is removed
before deleting scratch. Both pipeline processes and the output copy must
succeed before the source action publishes its output. Stock `oci_load`
produces the Docker archive with Make's source-hash and `latest` aliases; the
public archive label exposes only that tar file. The source action needs a
Docker-capable worker. Archive conversion needs no daemon and
uses the ordinary remote executor. Neither archive construction nor loading
requires a GPU.

The Dockerfile producer retains network access and disables disk and remote
result caching because Dockerfile tags and package mirrors are mutable. A
cache hit does not qualify source construction.
`//test/rbe:source_image_miss_load_test` uses a marked Redis context with
the unchanged Dockerfile to check the resulting archive and Docker load.
Its recorded producer log must show the actual source build.
`source_image_docker_user_test` invokes the same builder helper as UID 65534
with the daemon socket's group, private writable state and no effective
capabilities. This checks Docker-user compatibility, not Docker isolation.
The ordinary
`source_archive_load_test`, `stable_diffusion_archive_load_test`,
`triton_archive_load_test`, `ollama_archive_load_test`,
`ollama_singlemodel_archive_load_test`, `ollama_bench_archive_load_test` and
`tpu_vllm_archive_load_test` check the loaded tags, config and ordered layer
identities without starting application containers. The Ollama benchmark and
TPU vLLM archives have explicit source targets because Make excludes them from
its routine image checks.

The Ollama benchmark image instead packages its six checked-in raw model
manifests with their immutable blobs and the pinned Ollama 0.40.6 server. A
small repository rule reads only manifest metadata; curl downloads the blobs
in declared remote actions, which verify their SHA-256 and byte size. Each
model is packaged by `pkg_tar` directly into a gzip layer. `oci_image` and
`oci_load` supply the same archive interface, with an image-digest tag and
`latest`. No server runs during construction, so the image does not include
a generated private key.
The manual `ollama_small_archive_load_test` uses the smallest existing model,
checks archive identity, and runs the actual server's list/show commands
without network access. It qualifies packaging and model discovery, not GPU
inference. The benchmark remains excluded from routine image cohorts.

Large source builds request 256GB disk and 12GB memory; conversion requests
80GB disk and 12GB memory. Ollama's raw blobs total 92.2GB. Its largest model's
packaging action needs raw bytes plus the compressed tar, while the final
archive needs all compressed input layers plus its output. Docker load also
keeps a temporary extracted archive beside expanded layers. Its existing
512GB load request remains a request; the full image is not qualified by a
small-model pass or by the provider's roughly 200GB root cap. TPU vLLM uses the
ordinary 80GB source/conversion allowance and requests 80GB for its load
check. These are requests, not guaranteed capacity or measured source-build
requirements.
Native Triton's 48.5GB archive expands to 70.0GB of layer tar streams,
including tar metadata and historical layers. Its full load check reports a
69.9GB expanded Docker image and peaked at 119.7GB used on a 198.3GB root
filesystem. For images built with Dockerfiles, BuildKit cache, snapshots and
the exported layout coexist; their source-build peak remains a separate
qualification result.

The existing-driver GPU smoke suite can run on a dedicated Linux AMD64 host
with an installed NVIDIA driver and Container Toolkit:

```sh
bazel test --config=rbe-matrix --config=x86_64 --config=rbe-local-tests \
  --run_under=//test/rbe:local_root \
  //test/gpu:existing_driver_smoke
```

Run this command as an unprivileged user with passwordless sudo on a disposable
host. No other Docker daemon or GPU test may run on that host during this
command: the root fixture creates a private mount namespace but shares the host
network namespace, including Docker's bridge and firewall state. The existing
root fixture needs mount-namespace and cgroup access. Docker,
containerd, runc and `nvidia-container-runtime-hook` must be installed on the
effective PATH inherited by the root test process. [Docker discovers the NVIDIA
hook through that PATH](https://github.com/moby/moby/blob/fca702de7/daemon/nvidia_linux.go#L32-L46)
when registering its GPU DeviceRequests driver; the fixture does not register
or install a separate NVIDIA runtime. The host must expose the control and
assigned GPU devices needed by that driver; assignment need not start at index
zero. Docker and the existing native `TestNvidiaSmi` exercise actual device access
and report toolkit or driver incompatibility.

Compilation and the two declared CUDA source-image archives use the existing
remote build graph. The three test actions run locally, one at a time, without
test-result caching: native-runc smoke, runsc smoke with `--nvproxy=true`, and
runsc GPU checkpoint/restore with the same flag. Each uses the existing private
Docker daemon and cleanup, its declared images and release inputs, and the raw
suite's 300-second default action timeout. The frontends prohibit remote and
sandbox execution. The two source-image producers retain their networked build
behavior; graph analysis alone does not establish that those builds or any GPU
test ran.

The `existing_driver_all`, `existing_driver_l4`, `existing_driver_cos`, and
`existing_driver_cuda_12_8` suites expose the other original runtime commands
without replacing the installed driver. Ubuntu full and L4 tests use all driver
capabilities; COS keeps only `--nvproxy=true` and passes `--cos-gpu` to each raw
test. COS retains the public disabled-ffmpeg test and its existing driver paths
under `/home/kubernetes/bin/nvidia` or `/var/lib/nvidia`. Its device helper and
the raw tests' GPU-count helper retain their existing contiguous-index
assumptions; these declarations do not generalize device enumeration. CUDA12.8
includes both CUDA images because its version probe uses the older image too.
Eight independent CUDA frontends pass the existing one-based partition flags;
they do not shard the single top-level Go test with Bazel.

The complete public cohorts have ordered host actions, including the original
latest-driver installation and smoke/checksum phases. On an exclusively owned,
disposable host meeting the prerequisites above, use the same remote compilation
and serial local execution flags and select one action:

```sh
bazel test --config=rbe-matrix --config=x86_64 --config=rbe-local-tests \
  --run_under=//test/rbe:local_root --test_timeout=3300 \
  //test/gpu:public_smoke
```

| Public cohort         | Declared action                                          | Original job budget        |
| --------------------- | -------------------------------------------------------- | -------------------------- |
| Smoke                 | `//test/gpu:public_smoke`                                | 3300 seconds               |
| Full Ubuntu           | `//test/gpu:public_all`                                  | 5400 seconds               |
| L4                    | `//test/gpu:public_l4`                                   | 3300 seconds               |
| COS                   | `//test/gpu:public_cos`                                  | 3300 seconds               |
| CUDA12.8              | `//test/gpu:public_cuda_12_8_partition_1` through `_8`   | 5400 seconds per partition |
| All supported drivers | `//test/gpu:public_all_drivers_partition_0` through `_7` | 3300 seconds per partition |

Set `--test_timeout` to the table's budget for the selected host action. These
are the public coordinator budgets, distinct from the individual existing-driver
frontends' 300-second defaults and CUDA's eternal timeout. The complete host
action runs the built commands directly, matching Make's serial phase order;
it does not start nested Make or Bazel or inherit separate TestRunner timeouts
for its child commands. Each child clears the outer Bazel timeout, filter,
fail-fast and shard environment; CUDA retains its explicit sample partition
arguments. The outer action deadline still bounds the complete sequence. Build
time is outside the host action's budget.

Except for COS, the public actions may replace the host's kernel driver using
`tools/gpu:main`. The host must have the CLI's existing DKMS, matching kernel
headers and compiler prerequisites, plus network access to the registry's
checksum-verified NVIDIA installers. No device user or other workload may run
on that host. A test does not restore its original driver afterward: discard
the host after qualification, including after cancellation or installation
failure. COS uses its preinstalled driver and never invokes the installer.
The three Docker smoke frontends finish cleanup before another driver install
starts. No new worker pool, scheduler property or provisioning is implied.

The all-driver actions share the original script's sorted architecture-filtered
registry and zero-based modulo-eight partitioning. `drivers.tsv` records each
successful installation, failed installation, smoke failure and pass. Empty
partitions are explicit; failed installs are skipped, and a nonempty partition
must successfully test at least one driver. A passing wrapper therefore does
not prove that every assigned driver ran. Per-step output directories and child XML paths preserve
the individual fixture outputs without overwriting the outer action report. Existing CUDA-version and checkpoint/restore
skips remain visible outcomes.

All runtime binaries, release inputs, two CUDA image archives and eight other
GPU image archives are declared. The common basic and ffmpeg archives reuse
their existing pinned producers. Dockerfile-backed builds need Docker and
network access but no GPU; the Ollama client uses the standard OCI rules to
assemble its pinned base and Bazel-built executable without a build daemon.
Building those outputs or analyzing these actions provides no hardware
execution credit; all six public hardware cohorts remain unqualified until
they run on appropriate hosts.

The maintained Docker runtime lane has an explicit action-owned setup:

```sh
bazel test --config=rbe --config=x86_64 \
  //test/docker:owned_tests
```

The suite selects the same five test source sets as `make docker-tests`, with
all eight runtime configurations from `test/docker/config.bzl`. Each test
binary starts one private Docker daemon before its tests, loads only that
suite's declared image archives, and stops the daemon after all parallel tests
finish. The source-built release retains strict sidecar lookup. The former
single-case lifecycle target is covered by the maintained integration suite.

The owned suites use declared source-image producers for AMD64 and ARM64.
These build the canonical Dockerfiles and supply Make's source-hash tags through
stock archive conversion. Explicit source aliases share the nginx, containerd
harness and Moby producers. Image names and cohorts remain owned by
`test/docker/config.bzl`. The archive rule retains its explicit registry-image
compatibility option; the generic owned suites select source production by
default. Kubernetes' third-party kind/node image remains an explicit registry
dependency because this repository has no corresponding Dockerfile.
Docker-in-Docker cases still pull and build images over the network. They are
not hermetic tests, and their external failures remain visible.

Owned tests request root Firecracker workers with the pinned provider image
that supplies Docker, external networking and IPv6. They use the host
OverlayFS driver and request root disk space for expanded image layers and
container writes; the action workspace holds the declared archives. These
resource estimates need runtime qualification. Test logs identify the actual
Docker version, storage and cgroup mode. ARM64 Firecracker capacity remains
unqualified; providing ARM64 image pins does not supply that capacity.

The `docker-v1` lane selects the same complete AMD64 Docker suite under an
actual cgroup-v1 hierarchy:

```sh
test/rbe/qualify.sh docker-v1
```

It adds `--run_under=//test/rbe:cgroup_v1` before each existing test entrypoint,
so the wrapper binds unused controllers before the test starts its private
Docker daemon. It preserves the command vector, test selection, runtime table
and sharding. It does not infer the hierarchy from
`CGROUPV2` or replace a runtime check with that environment variable.

The remote wrapper route requires RBE and forbids local test execution.
Existing owned Docker actions select root Firecracker VMs with recycling disabled;
compilation keeps its ordinary execution platform. A private mount namespace
and child PID namespace contain the command. Cleanup removes only empty owned
groups, uses ordinary unmounts and checks the original mount view. Kernel
controller references may persist until the disposable VM is destroyed; their
residual state is recorded, without claiming global controller restoration.
The focused `//test/rbe:cgroup_v1_fixture_test` retains the existing
`TestCgroupV1` assertions separately from the generic wrapper.

The `unit-v1` lane uses the same wrapper and preserves `--config=unit`'s roots,
filters and exclusions. Remote v1 variants select the privileged setup
environment while compilation inherits the ordinary OCI platform. The local
container lane instead wraps only its KVM owner in the disposable guest
described above; its other owners retain their remote variants. Tests retain
their own memory settings. The live
NVIDIA checksum test also uses this environment, including its CA trust and
external networking, so setup can mount controllers before HTTPS requests.

Default `make docker-tests` and the original four test labels retain installed
Docker, staged bundles, custom `RUNTIME_BIN`/`RUNSC_TARGET`, `RUNTIME_ARGS`,
copy/sudo/reload behavior and partition variables. The installation adapter
uses the same runtime table as the owned daemon. `make docker-tests
DOCKER_TEST_SETUP=owned` selects the declared-input suite instead; installed
runtime overrides do not apply in that explicit mode. Direct owned runs should
set `PARTITION` and `TOTAL_PARTITIONS` through `--test_env` when partitioning.

The `overlay`, `swgso` and `hostnet` lanes run the same complete image and
integration suites as their public Make counterparts, with declared runtime
settings and the existing private daemon:

```sh
test/rbe/qualify.sh overlay swgso hostnet
```

Overlay uses `--overlay2=all:dir=/tmp`; the default Docker suite's separate
`all:self` tests remain unchanged. Software GSO uses `--software-gso=true
--gso=false`. Host networking uses `--network=host --net-raw` and the existing
test flags that disable checkpoint tests and identify host/raw networking.
These lanes preserve the public test settings. Their owning suites are
`//test/docker:overlay_tests`, `swgso_tests` and `hostnet_tests`; the original
`owned_tests` selection still runs only the default configurations. The public
pipeline selects AMD64 for these variants; ARM64 execution remains unqualified.
The installed Make lanes select their runtime flags from the same table while
retaining their custom binaries, runtime names, test environment and partitions.

The complete AMD64 port-forward lane uses the same owned daemon and declared
Redis/nginx archives. Its two existing tests run in separate sandbox-network
and host-network actions:

```sh
bazel test --config=rbe --config=x86_64 //test/root:portforward_test_owned
```

`make portforward-tests DOCKER_TEST_SETUP=owned` selects that suite. Default
`make portforward-tests` still installs each mode under the selected `RUNTIME`
using the caller's binary, arguments and daemon configuration. Both paths use
`PORTFORWARD_VARIANTS` in `test/docker/config.bzl`; the original installed test
remains the only Nogo owner. Declared ARM64 images do not supply ARM64
Firecracker capacity.

The sandbox-posture lane declares the six existing configurations from
`make sandbox-posture-tests` in `POSTURE_VARIANTS`: default, host networking,
host networking with raw sockets, no directfs, no directfs with host networking,
and KVM. Every configuration runs the existing `TestSandboxPostureDocker` and
`TestSandboxPostureDo` cases; their expected state comes from the selected
runtime's declared arguments. The ordinary root suite remains unfiltered.

The full target is `//test/root:sandbox_posture_test_owned`, also selected by
`make sandbox-posture-tests DOCKER_TEST_SETUP=owned`. It requires a worker that
can run KVM as well as create namespaces. The managed Firecracker worker has
not been qualified for nested KVM, so the full posture gate remains unsupported
there. The five non-KVM configurations can be qualified explicitly:

```sh
bazel test --config=rbe --config=x86_64 \
  --test_tag_filters=-requires-kvm //test/root:sandbox_posture_test_owned
```

This selects ten test cases and leaves the two cases in
`//test/root:sandbox_posture_test_kvm_owned` unqualified. Do not report that
partial result as a passing full posture gate. Default `make
sandbox-posture-tests` retains the complete six-configuration installed workflow
using the same configuration table, staged/custom runtime and caller arguments.

The startup lane runs the existing presubmit benchmark smoke workload:

```sh
test/rbe/qualify.sh startup
```

It selects `BenchmarkStartupEmpty` with `-test.benchtime=1ns` on ptrace, systrap
and native runc. Each action owns its Docker daemon, declared runsc binary and
the existing `benchmarks/alpine` image. Runtime variants come from the public
platform definitions, with the same `--profile` setting as Make. The full
`//test/benchmarks/base:startup_test_owned` suite also declares KVM; that variant
remains unqualified on hosted Firecracker workers and is excluded by this
profile's `-requires-kvm` filter.

This is a functional smoke check, with no benchmark uploads or performance
qualification. Failures remain errors, as in the presubmit pipeline. The
separate master-only performance benchmark jobs retain their soft-fail policy.
The installed `startup_test` entrypoint and `make benchmark-platforms` retain
their custom runtime selection and full benchmark arguments.

The separate continuous benchmark lane uses the master pipeline's maintained
workloads:

```sh
test/rbe/qualify.sh benchmarks
```

`//test/benchmarks:continuous_tests` declares the 20 Docker workload selections
on systrap, KVM and native runc, plus the direct OCI and shim lifecycle
benchmarks on systrap and KVM. The hosted lane selects AMD64 systrap and runc:
42 actions in all. Its `-requires-kvm` filter leaves 22 KVM actions
unqualified. The full suite remains available to workers with KVM support.
The existing startup smoke targets and their image cohort are separate.

Each benchmark declaration retains the continuous pipeline's filter and
benchtime: one iteration for build/media/Ruby-development workloads, 1000
iterations for each FIO selection, 30 iterations for lifecycle, and the
existing 30-second default elsewhere. TensorFlow retains its own fixed-run
harness behavior. The benchmark bodies still own cache dropping, FUSE setup,
checkpoint support checks and their existing skips. A skipped workload is not
qualified by a passing test wrapper.

Docker actions share the existing owned fixture, load only their declared
images and request four CPUs, 8GB memory and 40GB disk. These are allowances
for the full workloads, not measured minimums. Continuous targets allow up to
one hour per action; the hosted coordinator's shorter shared deadline can
still leave a cohort incomplete. Both lifecycle suites use their existing
benchmark bodies and the namespace worker with an 8GB memory allowance. The
shim suite declares the full release, including containerd-shim-runsc-v1, and
uses its own TTRPC fixture; neither suite needs a Docker or containerd daemon.
The installed benchmark entrypoints remain available.

Runsc variants enable `--profile` as Make does, and explicitly disable the
ordinary test fixture's debug logging. No profiler starts by default; optional
existing profile arguments remain available on individual targets. The images
are the canonical `tools/images.mk` source-hash releases pinned by digest.
ABSL, syscallbench and TensorFlow have only AMD64 image contexts and their
owned actions reject ARM64. Other image pins include ARM64 where published;
that does not qualify ARM64 execution capacity or the continuous ARM64 matrix.

The Buildkite benchmark jobs retain their soft-fail policy. This diagnostic
lane keeps all benchmark failures visible and returns a failing status while
`--keep_going` collects independent results. It does not upload benchmark data,
change the production benchmark pipeline, or establish a performance baseline.

The containerd lane runs the full existing CRI tests against containerd 1.7.31,
2.0.8, 2.1.7 and 2.2.3:

```sh
test/rbe/qualify.sh containerd
```

Each version owns an outer Docker daemon that loads only the
`containerd/harness` image. On AMD64, all four versions depend on one declared
archive from `//test/docker:containerd_harness_source_amd64`. That build action
uses the canonical Make cache/build selection and the shared OCI producer
described above, preserving the source-hash and canonical `latest` tags. The
ordinary test fixture loads the archive into its separate daemon; no registry push or
per-test image compilation is required. ARM64 retains its existing image pin.

The source archive is not hermetic: Dockerfile base tags and package mirrors
are mutable network inputs. The producer disables disk and remote result
caching, while normal compilation caches remain enabled. Bazel's incremental
state may still reuse an unchanged producer; an actual construction claim
requires its execution record, not just successful consumers. Qualification
records the concrete archive and image identity. A canonical `load-*` may pull
an already published matching source-hash tag instead of compiling it.

The harness receives the source-built release and
imports the six declared workload archives directly through CRI's existing
image import path. It also imports the selected containerd version's default
pause image before creating pods. These workload and pause archives keep their
AMD64 and ARM64 pins; the selected version and archive contents are test inputs.

The full suite is `//test/root:crictl_test_owned`. The installed `crictl_test`
target and `make containerd-tests` retain custom runtime selection, Docker
image export and the existing version flags. Harness output, test status and
both container and daemon cleanup failures reach the outer test result.
These tests require the worker's namespace, cgroup and CNI kernel support;
missing capabilities remain failures. ARM64 execution requires Firecracker
capacity. Optional shim-grouping measurements are separate from the public
containerd test jobs.

The bwrap lane runs the existing integration suite directly:

```sh
test/rbe/qualify.sh bwrap
```

Its declared release provides runsc and its sidecar binaries. The test requests
a root Firecracker worker for nested namespaces and cgroups, inheriting the
pinned Ubuntu image and its standard shell, coreutils and hostname utilities.
It binds that worker filesystem into the sandbox and uses a test-owned runtime
directory. No Docker daemon or container image archive is needed. The complete
test selection retains its existing unsupported user-namespace joining case.
`make bwrap-tests` continues to pass its staged or custom `--runsc` executable.
The ARM64 suite can use the Actions frontend with `lanes=bwrap`,
`architecture=arm64` and `execution=local`; compilation still uses RBE.
Fully remote ARM64 execution requires a worker with the same namespace
and cgroup support.

The packetimpact lane selects the same complete suite as `make packetimpact-tests`:

```sh
test/rbe/qualify.sh packetimpact
```

`//test/packetimpact/tests:all_tests` runs each testbench against both native Linux
and gVisor. Its existing timeouts, multi-DUT cases and expected netstack failures
remain owned by `test/packetimpact/runner/defs.bzl`. The runner declares its
testbench, POSIX server and source-built release with sidecars, and creates its
own user and network namespaces, veth links and packet captures.

For declared negative tests, a passing wrapper requires a failing child.
The qualification verdict retains those child failures in `case_counts` and
`expected_failures`, separately from unexpected `failures`. They establish the
negative-test contract, not support for the failing behavior. Missing cases,
unexpected child success and failed wrappers still prevent qualification.

Remote wrappers request root Firecracker workers with IPv6 and the existing
pinned networking image, which supplies `iptables-nft` and `ip6tables-nft` for
the runner's TCP filtering. Worker namespace and nftables support remain runtime
requirements. The public step specifies Ubuntu, cgroup v2 and a modern kernel,
without an explicit architecture selector. Combined selection uses AMD64;
standalone ARM64 execution requires Firecracker capacity. The other network
conformance suites have separate lanes described below.

The language runtime lanes retain the five public AMD64 suites: PHP 8.5.11,
Java 21, Go 1.22, Node 26.12.0 and Python 3.12.3. DirectFS matches presubmit;
goferfs matches the continuous matrix:

```sh
test/rbe/qualify.sh language-directfs
test/rbe/qualify.sh language-goferfs
test/rbe/qualify.sh --arch=all unit language-directfs language-goferfs
```

Each action starts the shared private Docker daemon and loads its language's
declared archive. Each language uses the shared source-image producer and
canonical `tools/images.mk rebuild-*` recipe. The producer builds one archive
for both filesystem modes instead of exporting a published image. Mutable base
tags and package mirrors retain the existing uncached producer action policy.
The original image entrypoint is retained, including Node's `dumb-init`.
The declared release and proctor run the existing tests with systrap and
`--watchdog-action=panic`. Installed `make %-runtime-tests` entrypoints still
accept their current runtime, image, partition and test controls.

The owned suites schedule the public CI partitions inside Bazel. Each partition
keeps the existing four or eight Bazel shards, batch size, exclusions and runner
timeout; each test owner sets its 1800-second action timeout. Java's forty
partitions cannot be collapsed into one test target because Bazel limits each
target to fifty shards. Each complete mode schedules 456 actions across 64
partition targets. No external `PARTITION` or `TOTAL_PARTITIONS` setting is
needed for these owned suites. Selecting a concrete partition target, such as
`//test/runtimes:go1.22_directfs_1_owned`, provides only partial coverage.

`RUNTIME_TESTS_FILTER`, `RUNTIME_TESTS_PER_TEST_TIMEOUT`,
`RUNTIME_TESTS_RUNS_PER_TEST`, `RUNTIME_TESTS_FLAKY_IS_ERROR` and
`RUNTIME_TESTS_FLAKY_SHORT_CIRCUIT` use the defaults in the runtime runner.
The dispatcher forwards only variables set in its environment, including empty
values. Unset controls leave any user rc `--test_env` settings in effect; a set
empty value selects the runner's default. Explicit controls apply to the whole
combined invocation, but only the language runner consumes these names.
Make retains its explicit value transport through the builder, and direct Bazel
commands retain their existing `--test_env` precedence.
The language and Kubernetes test owners retain their 1800-second deadline
even when an invocation overrides `--test_timeout`; other tests retain the
invocation's timeout settings.
An explicit test filter replaces the exclusion list as before. Filtered or
partition-only results do not qualify a complete language lane. These published
images and public jobs are AMD64-only; selecting an ARM64 language lane fails
explicitly. Passing a bounded cohort does not qualify all language partitions.

Java discovery and execution both apply jtreg's existing `-tl:200` policy:
tests declaring a timeout of zero or over 200 seconds are excluded. This is a
coverage limit, not evidence that those test bodies passed. Qualification
reconciliation retains the unfiltered inventory and exact excluded names when
comparing an earlier unfiltered discovery with this policy. The reporter reads
jtreg's framed results separately from arbitrary test stdout and records
`Passed. Skipped:` results as skips, including their original reasons.

The rootless Podman lane runs the public smoke workload with DirectFS disabled
and enabled:

```sh
test/rbe/qualify.sh podman
```

The full `//test/podman:podman_test` owner runs directly as the `nonroot` user in
a Firecracker worker using the existing pinned `basic/podmantest` image. It
loads the declared Alpine archive and uses the source-built release with strict
sidecar lookup; it does not install packages, pull a workload image, or start a
Docker daemon. Podman retains its default rootless networking and the test
requires cgroup v2 and actual subordinate UID/GID mappings. Private HOME and
runtime/storage directories avoid the image's permissive
`ignore_chown_errors` configuration and isolate cleanup from other containers.

The installed `test/podman/run.sh` adapter retains its host package and runtime
setup and delegates to the same smoke commands. By default that body owns both
DirectFS modes; `RUNTIME_ARGS` retains the original single configured run and
its shell quoting, including explicit `--directfs` selection.
It can also run directly with an installed runtime and image archive, as
described by `test/podman/smoke.sh`'s usage. Both architecture images are pinned,
but ARM64 Firecracker capacity remains unqualified. Image tools, strict rootless
storage and default networking must work on the actual worker; failures are not
converted to skips or host-network runs.

The small infrastructure smoke suite covers ordinary Go and C++ test actions
plus a syscall test on native Linux and gVisor's systrap platform:

```sh
bazel test --config=rbe --config=x86_64 //test/rbe:smoke
```

The execution platform pins an official Ubuntu 22.04 action image. Ordinary
actions use OCI isolation; native and systrap syscall tests request Firecracker
VMs as root so the existing runner can create its nested namespaces. Their
per-test properties inherit the image and architecture from the execution
platform. Compilation and tests use remote execution without local fallback.
Normal incremental caching is retained.

For qualification evidence, disable cached test results and retries with
`--nocache_test_results --flaky_test_attempts=1`, and inspect the invocation's
test results and execution records. A cached result or a coordinator-local test
is not evidence of remote test execution. Keep logs in the hosted invocation;
do not upload raw build-event files containing authentication options.

This smoke suite does not select the complete unit, syscall, Docker or
containerd suites. Use their owning lanes for those selections. Worker
requirements and existing test failures still apply to each lane; Firecracker
alone does not supply KVM, accelerators or alternate kernel environments.

The Kubernetes smoke lane owns one kind cluster inside the shared Docker
fixture:

```sh
test/rbe/qualify.sh kubernetes
```

The `//test/kubernetes/tests:kind_test` owner declares kind 0.33.0, its
Kubernetes 1.37.0 node image, the existing Alpine archive and the source-built
release. It does not download tools or pull workload images during the test.
The native runc node runs the normal
kind bridge and CNI; a gVisor RuntimeClass selects the declared runsc and
strict sidecars for the existing hello workload. The native sanity check and
hello use the same declared-image policy. The test deletes its unique cluster
before the shared fixture stops Docker, and cleanup failures fail the test.

The original `hello_test` remains usable with an external cluster through
`kubectlctx`, without requiring Docker. `make kubernetes-smoke-test` retains
its existing local setup. Migrating that adapter requires a supported way to
run the owned daemon as root while preserving caller-owned builds, declared
runfiles and process cleanup; the current Make builder does not supply it.
The kind lane selects AMD64 only. The worker must support the real nested
cluster, networking and resource requirements; declaring the lane does not
supply those capabilities.

The portable Go binary retains both-architecture Nogo analysis. The native
test wrapper owns the AMD64 runtime inputs and does not forward Go coverage
metadata. Go coverage instrumentation is separate from the public Kubernetes
smoke job, which does not request it.

The networking lanes reuse the existing iptables, nftables and packetdrill
suites with declared image archives and a private Docker daemon:

```sh
test/rbe/qualify.sh iptables nftables packetdrill
```

The iptables lane includes the legacy client, nftables-compatible client and
Docker DNS-rule reproduction check. The nftables lane runs the existing Go
suite under both runc and runsc, plus the native netfilter syscall binary in
the nftables image. Their private bridges use the IPv6 configuration documented
by those suites. Runtime modes are shared with the installed Make adapters.
Existing unsupported nftables cases retain their explicit skips.

The full nftables lane can also run on an AMD64 Actions host through the existing
privileged Docker fixture, while compilation stays on RBE:

```sh
gh workflow run build.yml --repo tamird/gvisor \
  --ref "$QUALIFICATION_REF" \
  -f lanes=nftables -f architecture=amd64 -f execution=local
```

This route selects the same `//test/nftables:owned_tests` suite and loads
`nfnetlink` and `nf_tables` on the host, matching both public Make targets.
Each owner keeps its declared images and private IPv6-capable Docker bridge;
these tests exchange traffic within that namespace and need no outbound job
bridge. The root coordinator and Docker adapter provide the same private
PID/cgroup namespaces and disk-backed scratch as the other local Docker lanes.
Each test retains its original 900-second timeout; the Actions step allows
45 minutes, with a 50-minute job limit. Results must account for actual
IPv4/IPv6 cases and skips, including native syscall cases printed to stdout
when the outer wrapper has no testcase XML.

Packetdrill retains all seven scripts against Linux and netstack. The wire
server and Linux DUT explicitly use runc; the netstack DUT uses the declared
runsc release. The shell tests share the Go suites' daemon lifecycle and report
command and cleanup failures. Kernel netfilter support remains a worker
requirement; missing features are errors. Native ARM64 images are declared,
but ARM64 Firecracker capacity remains unqualified.

The AMD64 website lane builds and checks the complete website filesystem and
packages its deployable image without a Docker daemon on the coordinator:

```sh
test/rbe/qualify.sh website
```

`//website:files` uses the existing published Jekyll tool image, pinned by digest
for remote execution, and the declared source build and HTMLProofer scripts.
It retains the generated documentation, future-dated pages, static overlay and
local-link checks from `make website-build`. `//website:image` packages the
same scratch filesystem with the Linux server, CA certificate, `/server`
entrypoint and port 8080. The image contains the website payload, not the
Jekyll tool environment.

The installed `make website-build`, `website-server`, `website-push` and
`website-deploy` adapters retain their Docker import and selected image name.
The remote lane builds the artifact; it does not publish or deploy it. The
public website job is AMD64, and this lane makes no ARM64 qualification claim.

The `go-export` lane builds the published Go module on Linux AMD64, then
cross-builds the public netstack package selection for Darwin ARM64, Windows
AMD64, FreeBSD AMD64, OpenBSD AMD64 and Linux MIPS. All compilation runs in
Bazel actions using the declared SDK; native Linux retains cgo and the selected
C toolchain. Dependencies come from the original `go.mod` and `go.sum`, fetched
at repository resolution and supplied as a local module proxy. Build actions
cannot fetch modules or modify either manifest. This lane uses the same export
archive as `tools/go_branch.sh` without modifying Git branches.

The save/restore and save/resume syscall lanes use the same generated tests and
positive tags as the public continuous jobs:

```sh
test/rbe/qualify.sh syscalls-save
test/rbe/qualify.sh --arch=arm64 syscalls-resume
```

These reuse `test/syscalls.targets` and the runner's supported platform selection.
They do not change the default `syscalls` lane, which excludes save variants.
The public jobs exercise save/restore on AMD64 and save/resume on ARM64. ARM64
namespace owners can use the local Actions executor with
`--arch=arm64 --test-execution=local syscalls-resume` and the existing hash15
buckets. Remote execution still requires Firecracker capacity and fails when
those workers are unavailable. Declaring a lane does not establish that
its tests pass or complete the full save/restore matrix.

With `--arch=all`, both checkpoint lanes use those public architectures and can
join ordinary syscalls and other lanes in one invocation. Each profile is
selected separately by Bazel before applying the existing worker-capacity
checks. Unavailable ARM64 owners are reported; a selection with no available
tests fails before execution.

```sh
test/rbe/qualify.sh --arch=all syscalls syscalls-save syscalls-resume smoke
```

Checkpoint combinations use explicit runtime owners so the ordinary syscall
profile's `-allsave` filter cannot discard requested checkpoint tests. Their
filtered tests' build-only work remains in the standalone invocations.

The Syzkaller lane runs the upstream smoke script against the declared gVisor
release:

```sh
test/rbe/qualify.sh syzkaller
```

`//test/syzkaller:smoke_test` uses a pinned Syzkaller source archive and its own
Go module graph. Its build action runs upstream Make with declared Go, LLVM,
GNU sysroot and Make inputs; the smoke action uses those build outputs and the
source-built release. The pinned source replaces the existing Make target's
moving clone, so newer upstream changes require a pin update.

The existing smoke script owns its workload and cleanup. It requires a root
AMD64 Firecracker worker and the pinned Syzkaller utility image; compilation
uses the declared tools. This lane exercises compatibility with that workload,
not a sustained fuzzing campaign or a staged release archive. The public
Syzkaller jobs and this target are AMD64 only.

### Mixed target architectures

`architectures = ["amd64", "arm64"]` on maintained test declarations adds explicit
`<name>_amd64` and `<name>_arm64` test variants. Each variant
uses the original declaration's arguments, environment, data, sharding, and
execution properties. The original target and its Nogo analysis remain available;
Nogo already analyzes both architectures. Variants are manual to avoid changing
existing broad target selections.

On a Linux remote coordinator, the initial mixed-language selection is:

```sh
bazel test --config=rbe-matrix //test/rbe:platform_matrix
```

This builds and runs two ordinary Go/C++ owners for both architectures in one
Bazel invocation. The existing test frontend selects a matching native worker;
compilation and linking retain the caller's execution constraints and consistent
toolchain selection. No global host-platform override is needed. Target-owned
`test.*`
properties continue to select specialized workers; declaring an ARM64 variant
does not supply the unavailable ARM64 Firecracker capacity.

The unit lane uses the same declarations without a copied test list:

```sh
test/rbe/qualify.sh --arch=all unit
```

The selector preserves `test/unit.targets`, including its exclusions and non-test
build targets, and adds ARM64 variants of its non-manual test owners. Both
architectures run in one Bazel test invocation with the ordinary unit tag
filters. A query of configured test actions reports and omits ARM64 Firecracker
requirements while that worker capacity is unavailable; the declarations retain
their ARM64 support. Shell, Python, YAML, generator, dependency and build checks
use the same architecture selection as Go/C++ tests. Single-architecture lanes
remain available through `--arch=amd64` and `--arch=arm64`.

The existing syscall runner also declares architecture variants centrally;
individual syscall declarations and their shard/hash buckets remain unchanged:

```sh
test/rbe/qualify.sh --arch=all unit release-repository syscalls
```

Bazel analyzes the ordered `test/syscalls.targets` roots with each public
architecture configuration, including the separate ARM64 64K-page systrap
profile, and `--build_tests_only`. The selector reads that
invocation's configured top-level test events, then checks the actual variants'
TestRunner configurations and worker properties. It reports KVM and ARM64
Firecracker omissions explicitly. It separately reports Nogo owners selected
by public CI but excluded by the established RBE runtime policy; the dedicated
`nogo` lane retains them. Public ARM64 syscall selection contains only
ptrace/systrap owners; it excludes native tests. Native syscall wrappers also
retain their existing privileged namespace fixtures, rather than being treated
as ordinary OCI tests.

The `syscalls-arm64-64k` configuration shares the public CI selection of systrap
owners without save/restore. It sets `--//tools/bazeldefs:page_size=64k`, replacing
`--define=pagesize=64k`. The setting accepts `4k` (the default) or `64k`; existing
page-size config labels still govern Go build tags and the systrap C trampoline.
Each selected owner has a manual `<owner>_64k_arm64` variant that configures its
runner, runtime and test dependencies together. Page size remains separate from
the CPU architecture interface, so ordinary and 64K variants can share a build.

The mixed selector maps this profile to manual `<owner>_64k_tcg` frontends.
They run the existing ARM64 payload inside a pinned 64K Linux guest using
QEMU full-system software translation on an unprivileged AMD64 OCI worker.
The guest checks its actual page size before running the payload; runsc retains
its own page-size check. Original arguments, shards and deadlines remain in
force, including guest boot and output transfer. This route needs no host KVM
or namespace privileges. See [the guest contract](tcg/README.md) for inputs,
resource estimates and output handling.

Use `test/rbe/qualify.sh --arch=arm64 syscalls-64k` for the separate profile,
optionally with `--syscall-bucket=0` for one existing hash15 partition or
`--syscall-bucket=1,2,3` for their union in one invocation. The same
comma-separated input is available as `syscall_bucket` in the workflow.
IDs must be distinct, increasing, and between 0 and 14; omitting the option
selects the full profile. Sets preserve the existing execution concurrency
and deadlines, so select a bounded group that fits the owning lane.
The report lists every unexecuted owner; declared routing is not a claim that
the whole profile has passed.

The `syscalls-rc` lane selects each requested CPU's public ordinary syscall
profile. ARM64 maps ptrace/systrap owners to manual `<owner>_rc_tcg` frontends.
Their existing `<owner>_arm64` payloads use 4K pages in the pinned Ubuntu RC
guest, with remote AMD64 OCI workers running QEMU TCG:

```sh
test/rbe/qualify.sh --arch=arm64 syscalls-rc
```

AMD64 maps native, ptrace, systrap and KVM owners from `syscalls-amd64` to
`<owner>_rc_kvm` frontends. They run on an AMD64 Actions host with nested KVM;
their existing `<owner>_amd64` payloads also use 4K pages. Both profiles can
share one invocation, with compilation remote and AMD64 guests serialized:

```sh
test/rbe/qualify.sh --arch=all --test-execution=local syscalls-rc
```

Use `--arch=amd64` for that profile alone. Either route accepts a set of existing
hash15 buckets and retains the owning arguments, shards and deadlines. Nogo
keeps its dedicated lane; the public profiles exclude checkpoint owners.
The report preserves each CPU's canonical selection and all unexecuted bucket
owners. Actions selects an AMD64 coordinator for this mixed RC lane, while
mixed unit tests retain their ARM64 coordinator. Local RC dispatches allow 90
minutes of work, 115 minutes for execution and evidence capture, and 120 minutes
for the job. The shared work deadline interrupts Bazel before the outer cutoff,
leaving time to retain failed and missing results; individual test deadlines
are unchanged.
Guest capabilities and full-profile timing require qualification;
declaring these routes does not establish that every selected owner can run.

The combined command keeps the original unit patterns and configuration,
including build-only tests and non-test targets. Selected syscall owners must
also survive those final filters; a conflicting selection fails before tests
start. A syscall-only command retains its original wildcard roots and RBE
filters. The runner owns the public `-Werror` compiler option, so syscall tests
retain that check without imposing it on unit or release compilation. Direct
runner invocations now receive the same compiler check; an existing trailing
`-Werror` is preserved without duplication.

### Moby network integration

The public AMD64 Moby lane runs its existing four shards with 360 seconds per
shard on the Actions cgroup-v2 Docker fixture:

```sh
test/rbe/qualify.sh --arch=amd64 --test-execution=local moby
```

Launch it through the workflow's `moby`, `amd64`, and `local` inputs so the
Actions entrypoint supplies the private network and credentials. The qualifier selects
`//test/moby:moby_owned`; it retains the three upstream test source files and
nine exclusions. Its private daemon loads the declared archive produced from
`images/moby/Dockerfile.x86_64` by the existing source-image action. The image
contains the source-selected Moby test binary and bundled test images. Source
image construction retains the public mutable registry/package inputs and
therefore requests no action cache reuse.

The runtime receives Make's raw-socket, packet-socket-write and nftables flags.
Each shard uses the existing private Docker network, cgroup-v2 namespace and
disk-backed scratch setup; compilation remains remote. ARM64 is not selected,
matching the public lane's outstanding ARM64 TODO. A discovery-only check is
not evidence that the nested Docker daemon or Moby test cases passed.


## Runtime clock selection

`test/rbe/qualify.sh --clock-source=reference LANE` explicitly selects host
reference clocks for runtime qualification. The default is `calibrated` and can
also be selected explicitly. The Actions `clock_source` input uses the same
setting. This does not change the production runtime default, alter profile
owners or deadlines, or turn reference-mode results into calibrated-mode credit.

The setting is carried as the declared test environment value
`GVISOR_TEST_CLOCK_SOURCE`. Shared syscall, Docker and Go runtime configuration
owners apply it before explicit fixture flags. Guest wrappers and maintained
nested-runtime frontends forward it to their existing runtime arguments or TOML
configuration. The same profile setting supplies a Bazel define for the direct
`runsc do` smoke owners' declared arguments. Native controls and ordinary
configuration-default unit tests retain their normal behavior. Selection artifacts record the chosen mode.

Reference mode is useful when raw hardware counters are not synchronized across
host CPUs. It uses the host clock for Sentry reads and the existing VDSO syscall
fallback; its additional cost must be measured on the deployment in question.
Calibrated-clock implementation tests remain separately exercised even when
runtime fixtures select reference mode.
