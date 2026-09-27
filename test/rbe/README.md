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
ARM64, Docker/containerd services, or kernel/cgroup variants. Firecracker alone
does not provide those capabilities. Missing capacity or failed tests must
remain visible failures rather than local fallback or additional exclusions.
