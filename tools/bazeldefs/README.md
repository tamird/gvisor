# Remote execution

`--config=rbe` uses BuildBuddy remote execution for compilation and tests.
It requires a BuildBuddy account with remote execution access and an API key.
Install the [BuildBuddy CLI](https://www.buildbuddy.io/cli/) and authenticate
from this repository:

```sh
bb login
```

The CLI saves the selected API key for this repository. Alternatively, supply
`BUILDBUDDY_API_KEY` in the environment, as described in the
[Remote Bazel authentication guide](https://www.buildbuddy.io/docs/remote-bazel/#authorization).

For example, to run an ordinary unit test and native and systrap syscall tests:

```sh
bb remote test --config=rbe --config=x86_64 \
  //pkg/atomicbitops:atomicbitops_test \
  //test/syscalls:uname_test_native \
  //test/syscalls:uname_test_runsc_systrap_shared
```

`bb remote` starts Bazel on a hosted coordinator, which supplies the
`buildbuddy_remote_executor` configuration and authentication. The repository
registers its execution platforms through `--config=rbe`, selects pinned
Ubuntu images and uses its declared compiler tools. The configuration is
opt-in; existing CI jobs must select it to use these workers. Remote failures
do not fall back to local execution.

`--config=rbe` prefers AMD64 execution workers; `--config=rbe-arm64` prefers
ARM64 workers. Both register the other architecture for tools that require it
and select a matching Bazel host platform for host-configured actions. These
settings select action execution, not the machine running the Bazel
coordinator. Select the target architecture explicitly with `--config=x86_64`
or `--config=aarch64`. For example, to run an ordinary unit test on ARM64:

```sh
bb remote test --config=rbe-arm64 --config=aarch64 \
  //pkg/atomicbitops:atomicbitops_test
```

Compilation and ordinary tests use OCI workers. Native, ptrace and systrap
syscall tests request root inside a Firecracker VM with IPv6 enabled. Explicit
test execution properties take precedence over these defaults. KVM and slimvm
require separate worker support, and an ARM64 compilation worker does not
establish ARM64 Firecracker availability.

The configuration allows 400 concurrent actions. Coordinator memory and remote
worker capacity still limit the number of actions that can run at once.
