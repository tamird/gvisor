# Stress tests on BuildBuddy

Use Bazel's repeated-test support on a BuildBuddy hosted Linux coordinator.
The repository's RBE configuration and test targets select the compilers,
worker images, isolation and resource requirements. No builder image or
separate stress runner is needed.

Publish the source commit, then submit a bounded run with the
[BuildBuddy CLI](https://www.buildbuddy.io/docs/remote-bazel/):

```sh
GIT_REPO_DEFAULT_BRANCH=master bb remote \
  --run_from_commit="$(git rev-parse HEAD)" \
  --os=linux --arch=amd64 \
  --runner_exec_properties=EstimatedCPU=4 \
  --runner_exec_properties=EstimatedMemory=16GB \
  --timeout=30m --disable_retry \
  --script='bazel --host_jvm_args=-Xmx8g --host_jvm_args=-XX:ActiveProcessorCount=4 test \
    --config=rbe-matrix --config=x86_64 \
    --runs_per_test=10 --nocache_test_results \
    --flaky_test_attempts=1 --noruns_per_test_detects_flakes \
    --keep_going --test_output=errors \
    //pkg/sync:sync_test //test/util:posix_error_test \
    //test/syscalls:clock_getres_test_native \
    //test/syscalls:clock_getres_test_runsc_systrap_directfs'
```

Use `--script`: the CLI's direct Bazel-command form may invoke local Bazel to
look up flags. `--run_from_commit` selects published source and disables local
workspace uploads. The outer deadline includes compilation and all test runs;
an incomplete run is not a pass.

Start with `--runs_per_test=1` when qualifying a new test or worker, then
increase it for stress testing. Select explicit targets instead of `//...`.
The example covers Go, C++ and native/gVisor syscall tests. Other declared
tests use the same interface. Mixed-platform targets retain their declared
architectures; selecting AMD64 as the default does not override their
transitions. See [remote qualification](README.md) for the available
platforms and worker gaps.

The shared RBE configuration permits 400 concurrent actions and disables
local fallback. Test timeouts and execution requirements remain target-owned.
Build caching stays enabled, but test results are not reused from cache and
failed tests are not retried. Any failed run fails the invocation; Bazel does
not turn mixed passing/failing repetitions into a successful flaky result.
The invocation records each run's status and log.

Use standard `--test_arg` flags for arguments understood by every selected
target: `--test_arg=-test.shuffle=on` for Go tests or
`--test_arg=--gtest_shuffle` for direct GoogleTest targets. Syscall wrapper
targets execute the Go syscall runner and do not accept GoogleTest flags
directly. Recorded shuffle seeds reproduce test order, not thread scheduling.

KVM, GPU and ARM64 Firecracker tests still require workers with those
capabilities. Repetition does not supply a missing execution environment.
