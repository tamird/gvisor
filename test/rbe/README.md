# Remote CI qualification

This is partial qualification of the remote execution environment. Run Bazel
on a BuildBuddy hosted Linux AMD64 coordinator against a published commit; its
`buildbuddy_remote_executor` configuration supplies the connection and
authentication. No custom gVisor builder image is required.

For authentication and individual remote build/test commands, see the
[remote execution guide](../../tools/bazeldefs/README.md).

See [stress testing](stress.md) to repeat selected Go, C++ or syscall tests
with the same remote configuration.

The CI-neutral entry point runs the implemented AMD64 lanes on that coordinator:

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
This profile does not replace the full public CI matrix. It omits the KVM
variants of posture, startup, continuous benchmarks and syscall tests, as well
as slimvm. Public CI uses AMD64 save/restore and ARM64 save/resume;
`--arch=all` follows that mapping. Standalone checkpoint lanes use the requested
`--arch`, defaulting to AMD64. ARM64 checkpoints require Firecracker capacity.
The root lane retains its cgroupfs owner and, on AMD64, also runs the complete
root suite under native systemd in a private PID, cgroup and mount namespace.
The test, Docker and runsc share that view and a writable delegated cgroup
subtree.
Container tests use their own runtime variants; their systemd case checks
serialized mock state and does not require a host systemd manager. KVM coverage
remains unavailable. All variants remain available through their owning Bazel
targets.

This document describes lane selection and environment requirements. Execution
results apply to the recorded source, selected tests and actual workers.
Declaring a lane does not establish that its tests pass, and failures remain
part of the qualification result.

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
| Public CI release-candidate syscall pools | AMD64 / ARM64 | Linux 7.3.0-070300rc3-generic, observed October 2, 2026; no equivalent RBE kernel selection is established. |

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
Supported immutable kernel selection and its effect on cache identity remain
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
The `lint-cc` lane calls `make lint-cc DOCKER_BUILD=false`, retaining its configured
compile actions and declared remote clang-tidy tool.

The `governance` lane runs `//governance:generated_files_test`. The existing
generator runs remotely with the complete indexed source tree, preserving
its area-directory checks. Two file-comparison tests check its outputs
against CODEOWNERS and MAINTAINERS.md without modifying the checkout.
Directory checks use indexed paths; an untracked directory cannot satisfy
them. This lane can share a mixed-platform invocation with the other tests.

The `license-check` lane calls `make license-check DOCKER_BUILD=false`
to compare the checked-in license catalog with the current dependency graph and
license policy. These three source lanes require the hosted AMD64 coordinator.
Their scoped Bazel configuration preserves the caller's rc files and does not
introduce a cache or output base.

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
Uploading results to GitHub is separate.

CodeQL reads every indexed source path using its current file contents, including
files outside the Bazel build graph. Stage new paths before analysis; missing
indexed files and sources resolving outside the checkout are errors. The input
is for analysis, not a source archive preserving Git modes or symlink metadata.
Go uses the declared SDK, C toolchain and a separate offline analysis module
profile. Gazelle supplies resolved archive pins for Go requirements declared
by the root `MODULE.bazel` and `go.mod`, including indirect requirements. The Go
SDK resolves their transitive requirements; Gazelle's own tooling dependencies
are not added as analysis roots.
The resulting manifests, proxy and license inventory are declared inputs. This
uses upstream module archives, without Bazel dependency source patches, and
leaves the source-export module profile unchanged. As in the public workflow,
`CODEQL_EXTRACTOR_GO_BUILD_COMMAND=:` skips dependency build heuristics without
restricting module discovery. Generated Go inputs are not overlaid; successful
extraction still needs its diagnostics and coverage assessed.

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
retains the `ConnectToSelf` filter on the image and integration suites. Only
the integration suite currently contains a matching test. As in Make, this
lane leaves the runtime platform at its default; its public agent's KVM
capability requirement does not set `--platform=kvm`.

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
  --strip=never --incompatible_sandbox_hermetic_tmp=false --keep_going
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
as `unit-v1`. The container test requests a root Firecracker worker for its
nested namespaces, but that does not provide the required KVM device. Both
full container lanes remain unqualified without hosted KVM capacity; missing
capabilities remain failures.

ARM64 unit qualification retains the full canonical selection. These owners
require Firecracker workers and remain unqualified without ARM64 capacity:

- `//runsc/cmd:cmd_test`
- `//runsc/sandbox:sandbox_test`
- `//sandboxexec/sandbox:sandbox_test`
- `//sandboxexec/sandbox/python:sandbox_py_test`

Their unavailable workers remain errors; passing OCI tests alone does not make
the full ARM64 unit gate pass. HTTPS tests use the existing multiarch networking
image for CA trust. Docker suites use a separate provider image that declares
both Linux architectures and supplies their daemon tools. ARM64 Firecracker
execution remains unqualified.

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
The lane preserves the public release test's default compilation mode; it
does not execute the ARM64 binaries or qualify native ARM64 runtime tests.

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
build's default compilation and stripping settings.

A declared OCI image supplies the release tools from an immutable Ubuntu Jammy
APT snapshot. The existing private Docker fixture owns the daemon, and native
runc executes the scripts with networking disabled. Only the declared scripts
and packages are mounted: no Git metadata or publishing script is present, so
the canonical release script generates its master repository. The test neither
publishes artifacts nor exercises tagged or nightly publication. Qualification
covers outputs built from source on RBE; externally produced staged release
bundles are outside its scope. Make's separate staged-binary support remains
available.

The CPU and GPU/ML image-source lanes retain the public manifest-or-build checks:

```sh
test/rbe/qualify.sh cpu-images gpu-images
```

They select the existing `tools/images.mk test-cpu-images` and `test-gpu-images`
owners through `//test/docker:cpu_image_sources_amd64_test` and
`//test/docker:gpu_image_sources_amd64_test`, or the corresponding `arm64_test`
targets with `--arch=arm64`. With `--arch=all`, both public profiles are analyzed
and their target-configured variants use the same worker-capacity selection as
smoke, Docker and bwrap. Each architecture runs natively, using the public
four AMD64 or two ARM64 partitions as Bazel test shards. Make retains image
discovery, complete-context hashing and the manifest check: a missing manifest
builds the image from its Dockerfile; a manifest hit does not prove a fresh
image build. Base-image and package downloads retain their network behavior.
The GPU/ML cohort includes Make's TPU images and preserves its `NON_TEST_IMAGES`
exclusions. These checks build images when needed; they do not run their GPU/TPU
workloads or require accelerator devices. ARM64 execution remains unqualified.

The action uses declared Make and crane tools with a private native-runc Docker
daemon. It needs no gVisor release or preloaded images. Complete contexts are
materialized from the declared `//images:source_contexts` archive so Make's
`find -type f` hash sees physical source files. The archive declares regular
file modes and the tracked executable overrides; this avoids Bazel marking
every remote input executable. The current contexts contain no symlinks; new
symlinks or executable files must be reflected in that archive declaration. The
legacy default Dockerfile remains an image-test subject, not a prerequisite
builder for the coordinator.

Each shard requests the existing Docker worker image, four CPUs, 8GB memory
and 40GB disk. These are capacity allowances, not measured minimums. The
declared one-hour test timeout does not override a shorter coordinator work
budget; cancellation leaves incomplete qualification. The image declares both
architectures, but ARM64 Firecracker capacity is still required and its
execution remains unqualified.

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

The archives pin the existing `tools/images.mk` source-hash tags by digest in
`MODULE.bazel`, for AMD64 and ARM64. Updating an image requires rebuilding its
existing Dockerfile and refreshing its corresponding digest. Image names and
cohorts are owned by `test/docker/config.bzl`; no alternate workloads are used.
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

The wrapper requires RBE and the lane forbids local test execution. Existing
owned Docker actions select root Firecracker VMs with runner recycling disabled;
compilation keeps its ordinary execution platform. A private mount namespace
and child PID namespace contain the command. Cleanup removes only empty owned
groups, uses ordinary unmounts and checks the original mount view. Kernel
controller references may persist until the disposable VM is destroyed; their
residual state is recorded, without claiming global controller restoration.
The focused `//test/rbe:cgroup_v1_fixture_test` retains the existing
`TestCgroupV1` assertions separately from the generic wrapper.

The `unit-v1` lane uses the same wrapper and preserves `--config=unit`'s roots,
filters and exclusions. All three v1 lanes select `--config=rbe-cgroup-v1`, which
gives test actions the privileged setup environment while compilation inherits
the ordinary OCI platform. Tests retain their own memory settings. The live
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
uses the existing native Docker daemon and canonical Make image load/build
rules, then saves the source-hash and canonical `latest` tags. The ordinary
test fixture loads the archive into its separate daemon; no registry push or
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
The public CI also runs this suite on ARM64; that worker lane remains
unqualified by this AMD64 profile.

The packetimpact lane selects the same complete suite as `make packetimpact-tests`:

```sh
test/rbe/qualify.sh packetimpact
```

`//test/packetimpact/tests:all_tests` runs each testbench against both native Linux
and gVisor. Its existing timeouts, multi-DUT cases and expected netstack failures
remain owned by `test/packetimpact/runner/defs.bzl`. The runner declares its
testbench, POSIX server and source-built release with sidecars, and creates its
own user and network namespaces, veth links and packet captures.

Remote wrappers request root Firecracker workers with IPv6 and the existing
pinned networking image, which supplies `iptables-nft` and `ip6tables-nft` for
the runner's TCP filtering. Worker namespace and nftables support remain runtime
requirements. The public step specifies Ubuntu, cgroup v2 and a modern kernel,
without an explicit architecture selector. Combined selection uses AMD64;
standalone ARM64 execution requires Firecracker capacity. The other network
conformance suites have separate lanes described below.

The language runtime lanes retain the five public AMD64 suites: PHP 8.3.35,
Java 21, Go 1.22, Node 22.2.0 and Python 3.12.3. DirectFS matches presubmit;
goferfs matches the continuous matrix:

```sh
test/rbe/qualify.sh language-directfs
test/rbe/qualify.sh language-goferfs
test/rbe/qualify.sh --arch=all unit language-directfs language-goferfs
```

Each action starts the shared private Docker daemon and loads its language's
declared archive. PHP uses the shared source-image producer; the other archives
are pinned to their existing `tools/images.mk` source-hash images.
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
namespace execution still requires Firecracker capacity; the lane must fail
when those workers are unavailable. Declaring a lane does not establish that
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

The mixed selector reports these required 64K owners as unavailable. No supported
hosted ARM64 64K-kernel worker configuration is known; ordinary ARM64 execution
and an OCI image do not establish that support. The declarations retain existing
ARM64 syscall worker properties without inventing a kernel route. Directly
requesting a 64K variant does not bypass runsc's existing fatal check that its
compiled page size matches the host kernel before booting the sandbox. Analysis
and cross-compilation can qualify the build graph, but cannot qualify this runtime
lane. Enable execution only after selecting and verifying a supported 64K worker.

The combined command keeps the original unit patterns and configuration,
including build-only tests and non-test targets. Selected syscall owners must
also survive those final filters; a conflicting selection fails before tests
start. A syscall-only command retains its original wildcard roots and RBE
filters. The runner owns the public `-Werror` compiler option, so syscall tests
retain that check without imposing it on unit or release compilation. Direct
runner invocations now receive the same compiler check; an existing trailing
`-Werror` is preserved without duplication.
