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
  //tools/buildbuddy:credential_helper_test //pkg/tcpip/seqnum:seqnum_test
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
action inputs. C++, cgo and external linking need action-specific restrictions
or a hermetic toolchain before using shared caching or remote execution. The
initial check above exercises pure Go and the credential helper; it does not
qualify those host-C actions or the complete correctness matrix.
