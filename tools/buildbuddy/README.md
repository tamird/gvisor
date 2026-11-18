# Shared Bazel execution and caching

The optional `buildbuddy` configuration enables remote execution and a shared
action cache. `buildbuddy-cache` enables only the shared cache. Both keep the
repository's target toolchains and architecture configurations.

Build the standard Bazel credential helper once:

```sh
bazel build //tools/buildbuddy:credential_helper
```

Set `BUILDBUDDY_API_KEY_FILE` to a private file containing the existing API key.
Keep the key itself out of Bazel's arguments and environment: Bazel 9.3.1 includes
expanded options and client environment values in its build-event records.

On a configured Linux worker, the initial Go-only integration check is:

```sh
bazel test --keep_going --config=buildbuddy --config=x86_64 \
  --credential_helper="remote.buildbuddy.io=$(realpath bazel-bin/tools/buildbuddy/credential_helper)" \
  --build_event_json_file="$RESULTS/events.json" \
  //tools/buildbuddy:credential_helper_test //pkg/atomicbitops:atomicbitops_test
```

`RESULTS` names the task's temporary result directory. Use `aarch64` in place of
`x86_64` for the ARM64 target configuration. Execution platforms select compatible
workers separately from target toolchains. A successful cross-compile alone does
not establish native ARM64 test execution.

Keep correctness results and compiled artifacts cached. For a required fresh
measurement, apply `--nocache_test_results` to the benchmark invocation; keep the
build cache enabled. Controlled before/after measurements remain separate from
the correctness suite.

The current Coral C toolchain does not declare its host compiler and sysroot as
action inputs. Native C++ actions and Go actions that can invoke absolute host C
tools stay local and out of the remote cache. The systrap host-tool generators
have the same restriction. Pure Go actions remain eligible for remote execution
and caching. Remove these exceptions when the hermetic toolchain declares and
qualifies the compiler, linker, sysroot and runtime-library inputs.

The initial check above exercises pure Go and the credential helper. Canonical
action-graph inspection and the affected correctness suites must also pass
before the profile is used for the full native and race matrix.
