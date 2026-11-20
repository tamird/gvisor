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

The execution platforms pin BuildBuddy's public Ubuntu 24.04 image by its
multi-architecture digest. Its userspace must load C/C++ binaries built on the
Ubuntu 24.04 build workers. Check the real C++ loader and namespace operations
before a full runtime build:

```sh
bazel test --keep_going --config=buildbuddy --config=x86_64 \
  --credential_helper="remote.buildbuddy.io=$(realpath bazel-bin/tools/buildbuddy/credential_helper)" \
  //runsc/specutils/safemount_test:safemount_test \
  //test/syscalls/coverage:uname_loader_test
```

The loader check runs the maintained Uname case directly, without building a
Sentry or release fileset. Repeat with `aarch64` for the ARM64 worker.

Keep correctness results and compiled artifacts cached. For a required fresh
measurement, apply `--nocache_test_results` to the benchmark invocation; keep the
build cache enabled. Controlled before/after measurements remain separate from
the correctness suite.

The `buildbuddy` configuration enables `//tools/bazeldefs:rbe_native_c`. Its C
toolchains require the target CPU and the pinned image execution environment.
C++ and Sysmsg actions cannot fall back to the submitting host. The image binds
the compiler and sysroot contents in the remote action key. Ordinary builds and
`buildbuddy-cache` retain the host cross toolchains and keep these host-tool
actions out of remote execution and caching.

Coral still derives its GCC include-directory version from the submitting
host. Its generated version must match the image's GCC 13 include directories.
Go actions that can invoke absolute C tools retain their `no-remote` exceptions
until their SDK execution architecture and C toolchain inputs are qualified.

The initial check above exercises pure Go and the credential helper. Canonical
action-graph inspection and the affected correctness suites must also pass
before the profile is used for the full native and race matrix.
