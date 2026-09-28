# Remote test qualification

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
excludes KVM posture and syscall variants, slimvm and syscall save/restore;
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

Select native ARM64 execution with `--arch=arm64`, for example:

```sh
test/rbe/qualify.sh --arch=arm64 unit
```

This uses the same lane selection and filters as AMD64. The `rbe-arm64`
configuration selects ARM64 build tools and execution workers; `aarch64`
selects the target architecture. Selecting an architecture does not establish
worker support or qualify the other lanes.

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
