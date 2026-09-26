# Stress tests on BuildBuddy

`remote_stress.sh` runs explicitly selected Bazel tests repeatedly on
BuildBuddy's remote executors. The Bazel coordinator also runs remotely. The
script refuses to run outside a BuildBuddy hosted runner.

Actions use a pinned official Ubuntu 22.04 image for the runtime environment;
the build supplies its compilers through the hermetic toolchains. BuildBuddy's
default Ubuntu 16.04 image is too old for the C++ runtime's glibc requirements.
Cache transfers use compression.

Install the [BuildBuddy CLI](https://www.buildbuddy.io/cli/) and authenticate with
`bb login`. Push the source commit to a repository accessible to BuildBuddy, then
run from that checkout:

```sh
GIT_REPO_DEFAULT_BRANCH=master bb remote \
  --run_from_commit="$(git rev-parse HEAD)" \
  --os=linux \
  --arch=amd64 \
  --runner_exec_properties=EstimatedCPU=4 \
  --runner_exec_properties=EstimatedMemory=8GB \
  --timeout=15m \
  --disable_retry \
  --script='tools/remote_stress.sh //pkg/sync:sync_test //pkg/waiter:waiter_test'
```

Use the `--script` form: the CLI can invoke local Bazel to discover flags for
`bb remote test`. `--run_from_commit` selects the published commit and disables
automatic uploading of local changes. See the
[Remote Bazel documentation](https://www.buildbuddy.io/docs/remote-bazel/).

Each target runs 100 times, with up to 32 concurrent remote actions and a
60-second timeout per test run. Pass `--env=STRESS_RUNS=10` to `bb remote` for a
shorter run. The outer 15-minute deadline includes building the tests. Test
results are not cached or retried, and any failing run fails the invocation.

Pass test binary arguments after `--`. For example, shuffle Go tests with:

```sh
--script='tools/remote_stress.sh //pkg/sync:sync_test -- -test.shuffle=on -test.v'
```

For GoogleTest binaries such as `//test/util:posix_error_test`, use
`-- --gtest_shuffle`. Pass only arguments supported by every selected test.
The BuildBuddy invocation contains every run's status
and test log, including shuffle seeds printed by the test framework. A shuffle
seed reproduces test ordering, not goroutine or thread scheduling. The hosted
runner's artifacts also include `stress.txt` with the source commit, selected
targets, repetition count, isolation type, and test arguments.

Syscall tests run through the repository's test runner. Select both a native
Linux target and a gVisor target to compare their behavior. For example, replace
the `--timeout` and `--script` arguments above with:

```sh
--env=STRESS_ISOLATION=firecracker \
--env=STRESS_RUNS=1 \
--timeout=30m \
--script='tools/remote_stress.sh //test/syscalls:clock_getres_test_native //test/syscalls:clock_getres_test_runsc_systrap_directfs'
```

This requests [Firecracker microVMs](https://www.buildbuddy.io/docs/rbe-microvms/)
for remote actions, running as root, because the gVisor runner creates nested
user and mount namespaces. Verify a single run on the selected workers before
increasing `STRESS_RUNS`. Pass no GoogleTest arguments to these wrapper targets:
their executable is the Go syscall runner, which does not forward those flags.
The longer outer deadline allows for a cold build of the runsc release files.

KVM or GPU tests need workers with those capabilities; Firecracker isolation
does not itself supply nested KVM or GPUs. Tests still need their appropriate
execution environment, regardless of language or test framework.
