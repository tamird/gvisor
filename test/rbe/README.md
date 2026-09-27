# Remote test qualification

This is partial qualification of the remote execution environment. Run Bazel
on a BuildBuddy hosted Linux AMD64 coordinator against a published commit; its
`buildbuddy_remote_executor` configuration supplies the connection and
authentication. No custom gVisor builder image is required.

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
  --strip=never --incompatible_sandbox_hermetic_tmp=false --keep_going \
  --test_env=CGROUPV2=true
```

Run this command from the workspace root without additional target arguments.
`test/unit.targets` owns the package roots and four existing exclusions; the
configuration retains the Nogo, KVM and plugin filters. It also retains the
non-test targets built by the existing wildcard selection.

The public unit matrix runs on AMD64 with cgroup v1 and v2, and on ARM64.
`CGROUPV2` preserves Make's environment marker; setting it does not select or
verify a remote worker's cgroup mode. The command above starts AMD64
qualification, not the complete architecture/cgroup matrix. This lane has no
separate race variant in the public pipeline.

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
that supplies Docker, external networking and IPv6. They use VFS and request
root disk space for expanded images and writable container copies; the action
workspace holds the declared archives. These resource estimates need runtime
qualification. Test logs identify the actual Docker version, storage and
cgroup mode. ARM64 Firecracker capacity and cgroup v1 remain separate gaps;
providing ARM64 image pins does not qualify either environment.

Default `make docker-tests` and the original four test labels retain installed
Docker, staged bundles, custom `RUNTIME_BIN`/`RUNSC_TARGET`, `RUNTIME_ARGS`,
copy/sudo/reload behavior and partition variables. The installation adapter
uses the same runtime table as the owned daemon. `make docker-tests
DOCKER_TEST_SETUP=owned` selects the declared-input suite instead; installed
runtime overrides do not apply in that explicit mode. Direct owned runs should
set `PARTITION` and `TOTAL_PARTITIONS` through `--test_env` when partitioning;
`--config=docker` preserves the lane's TCP save/restore setting.

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

This slice does not cover the complete unit/syscall matrix, KVM, GPU, native
ARM64, the complete Docker/containerd lanes, or kernel/cgroup variants. Firecracker alone
does not provide those capabilities. Missing capacity or failed tests must
remain visible failures rather than local fallback or additional exclusions.
