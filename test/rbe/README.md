# Remote CI qualification

This is partial qualification of the remote execution environment. Run Bazel
on a BuildBuddy hosted Linux AMD64 coordinator against a published commit; its
`buildbuddy_remote_executor` configuration supplies the connection and
authentication. No custom gVisor builder image is required.

The CI-neutral entry point runs the implemented AMD64 lanes on that coordinator:

```sh
test/rbe/qualify.sh amd64
```

Use `test/rbe/qualify.sh --list` to see the lanes and unqualified environments,
or pass lane names to run a smaller selection, for example
`test/rbe/qualify.sh unit portforward`. It runs every selected lane and returns
failure if any lane fails, including fixture cleanup after the test cases pass.
This profile does not replace the full public CI matrix. In particular, it
excludes KVM posture, startup and syscall variants, slimvm and syscall save/restore;
existing failures in selected tests remain failures. The host systemd cgroup
manager is also unqualified; container image tests that boot systemd do not
exercise that host service. All variants remain available through their owning
Bazel targets.

The dispatcher uses the existing Nogo and unit configurations, the declared
runtime suites, and the syscall roots shared with Make in `test/syscalls.targets`.
The lanes use separate Bazel invocations to preserve their different selections
and instrumentation. Connection settings and credentials come from Bazel's
configuration; the script does not install tools or start a builder container.
Normal Bazel caching remains enabled.

The `workflows` lane runs the declared actionlint check and the existing GitHub
and Buildkite schema tests. Actionlint uses the same workflow inputs as the
GitHub schema check; `tools/lint.sh actions` invokes that same Bazel owner.
Other source linters, governance checks, license headers and CodeQL remain
outside this lane.

Select ARM64 targets with `--arch=arm64`, for example:

```sh
test/rbe/qualify.sh --arch=arm64 unit
```

Test lanes use the same selection and filters as AMD64. Their `rbe-arm64`
configuration selects ARM64 build tools and execution workers; `aarch64`
selects the target architecture. The `release-artifacts` build lane instead
uses AMD64 execution workers for either target architecture. Selecting an
architecture does not establish worker support or qualify the other lanes.

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

ARM64 unit qualification retains the full canonical selection. Three owners
require Firecracker workers and remain unqualified without ARM64 capacity:

- `//runsc/sandbox:sandbox_test`
- `//sandboxexec/sandbox:sandbox_test`
- `//sandboxexec/sandbox/python:sandbox_py_test`

Their unavailable workers remain errors; passing OCI tests alone does not make
the full ARM64 unit gate pass. HTTPS tests use the existing multiarch networking
image for CA trust. Docker suites retain the separate AMD64 provider image that
supplies their daemon tools.

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
publishes artifacts nor exercises tagged or nightly publication. The separate
staged-binary check requires the real staged archive and remains unqualified.

The maintained Docker lane has an explicit action-owned setup:

```sh
bazel test --config=rbe --config=x86_64 --config=docker \
  //test/docker:owned_tests
```

The suite selects the same four test source sets as `make docker-tests`, with
all seven runtime configurations from `test/docker/config.bzl`. Each test
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
Docker version, storage and cgroup mode. ARM64 Firecracker capacity and
cgroup v1 remain separate gaps; providing ARM64 image pins does not qualify
either environment.

Default `make docker-tests` and the original four test labels retain installed
Docker, staged bundles, custom `RUNTIME_BIN`/`RUNSC_TARGET`, `RUNTIME_ARGS`,
copy/sudo/reload behavior and partition variables. The installation adapter
uses the same runtime table as the owned daemon. `make docker-tests
DOCKER_TEST_SETUP=owned` selects the declared-input suite instead; installed
runtime overrides do not apply in that explicit mode. Direct owned runs should
set `PARTITION` and `TOTAL_PARTITIONS` through `--test_env` when partitioning;
`--config=docker` preserves the lane's TCP save/restore setting.

The complete AMD64 port-forward lane uses the same owned daemon and declared
Redis/nginx archives. Its two existing tests run in separate sandbox-network
and host-network actions:

```sh
bazel test --config=rbe --config=x86_64 --config=docker //test/root:portforward_test_owned
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
bazel test --config=rbe --config=x86_64 --config=docker \
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

The containerd lane runs the full existing CRI tests against containerd 1.7.31,
2.0.8, 2.1.7 and 2.2.3:

```sh
test/rbe/qualify.sh containerd
```

Each version owns an outer Docker daemon that loads only the existing
`containerd/harness` image. The harness receives the source-built release and
imports the six declared workload archives directly through CRI's existing
image import path. It also imports the selected containerd version's default
pause image before creating pods. All archives are pinned for AMD64 and ARM64;
the selected version and archive contents are test action inputs.

The full suite is `//test/root:crictl_test_owned`. The installed `crictl_test`
target and `make containerd-tests` retain custom runtime selection, Docker
image export and the existing version flags. Harness output, test status and
both container and daemon cleanup failures reach the outer test result.
These tests require the worker's namespace, cgroup and CNI kernel support;
missing capabilities remain failures. ARM64 Firecracker capacity and the
separate shim-grouping performance lane remain unqualified.

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
without an explicit architecture selector. This profile starts with AMD64;
ARM64 Firecracker capacity and the other network conformance lanes remain
unqualified.

The language runtime lanes retain the five public AMD64 suites: PHP 8.3.7,
Java 21, Go 1.22, Node 22.2.0 and Python 3.12.3. DirectFS matches presubmit;
goferfs matches the continuous matrix:

```sh
test/rbe/qualify.sh language-directfs
test/rbe/qualify.sh language-goferfs
```

Each action starts the shared private Docker daemon and loads its language's
declared archive, pinned to the existing `tools/images.mk` source-hash image.
The original image entrypoint is retained, including Node's `dumb-init`.
The declared release and proctor run the existing tests with systrap and
`--watchdog-action=panic`. Installed `make %-runtime-tests` entrypoints still
accept their current runtime, image, partition and test controls.

The owned suites schedule the public CI partitions inside Bazel. Each partition
keeps the existing four or eight Bazel shards, batch size, exclusions and runner
timeout; the dispatcher retains Make's 1800-second action timeout. Java's forty
partitions cannot be collapsed into one test target because Bazel limits each
target to fifty shards. Each complete mode schedules 456 actions across 64
partition targets. No external `PARTITION` or `TOTAL_PARTITIONS` setting is
needed for these owned suites. Selecting a concrete partition target, such as
`//test/runtimes:go1.22_directfs_1_owned`, provides only partial coverage.

`RUNTIME_TESTS_FILTER`, `RUNTIME_TESTS_PER_TEST_TIMEOUT`,
`RUNTIME_TESTS_RUNS_PER_TEST`, `RUNTIME_TESTS_FLAKY_IS_ERROR` and
`RUNTIME_TESTS_FLAKY_SHORT_CIRCUIT` retain their Make defaults and meanings.
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

This slice does not cover the complete unit/syscall matrix, KVM, GPU, the full
ARM64 matrix, the complete Docker/containerd lanes, or kernel/cgroup variants.
Firecracker alone does not provide those capabilities. Missing capacity or failed
tests must remain visible failures rather than local fallback or additional
exclusions.

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
The first kind lane is AMD64 only; ARM64 kind execution and worker capacity
remain unqualified. The real nested cluster, networking and resource
requirements still need hosted validation.

The portable Go binary retains both-architecture Nogo analysis. The native
test wrapper owns the AMD64 runtime inputs and does not forward Go coverage
metadata, so Kubernetes Go coverage remains unqualified.

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
