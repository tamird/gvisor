# How gVisor should run lint

**Delete `tools/lint.sh`.** Give contributors and CI one read-only
command, `bazel build --keep_going //tools/lint:check`. Give contributors
one opt-in writing command, `bazel run //tools/lint:format`. These are
proposed interfaces. A temporary `make lint` alias can call Bazel but
should own no policy.

| Check | Where it belongs |
| --- | --- |
| Buildifier, goimports, clang-format | Independent source actions. The format command uses the same files, tools, configs and options as the checks. |
| cpplint, codespell | Read-only source actions with their declared Python interpreter, package-local lint configs and spelling dictionary. No automatic spelling edits. |
| Nogo, clang-tidy | On actual Go and C/C++ targets. A list of files cannot supply build tags, generated headers or target flags. |
| GitHub workflows | Keep the [existing actionlint test](https://github.com/google/gvisor/blob/0fa6803f2/BUILD#L138-L163) and its project configuration. |

## Reuse the tools and the action that runs them

Bazel handles parallelism, caching and failure reporting. Each supplier’s
binary owns diagnostics and formatting. gVisor owns file selection and
options. There is no reason to write one Go program per tool or put their
policies into a single program.

The [Aspect formatter test](https://github.com/aspect-build/rules_lint/blob/4cc8fe1da/format/defs.bzl#L143-L207)
uses a shell even with declared inputs. Its checkout mode also disables
sandboxing and caching. The [buildifier-prebuilt test runner](https://github.com/keith/buildifier-prebuilt/blob/2d23069a6/runner.bash.template)
invokes Bash and find. In [our comparison](https://app.buildbuddy.io/invocation/959cb581-ace8-4cfb-b95e-b88a2d1527f4)
it passed a malformed file that [Buildifier itself rejected](https://app.buildbuddy.io/invocation/3fde1b55-45f5-465b-9c65-90712fb2e9f7).

Use [bazel_lib’s native run_binary](https://github.com/bazel-contrib/bazel-lib/blob/904332fd1/lib/private/run_binary.bzl#L39-L72)
to call Buildifier directly. On an empty AMD64 worker it [accepted clean
source](https://app.buildbuddy.io/invocation/90cfd7a5-d611-4dd5-9fd8-9554e0f03e09)
and [rejected the malformed file](https://app.buildbuddy.io/invocation/f5b006bf-2c5b-4fef-b3b9-f120a2f7d204),
with no shell. gVisor already uses bazel_lib in its production build;
taking 3.7.2 is a shared upgrade from 3.2.2. For goimports, capturing
stdout alone is insufficient: it lists bad files but exits zero, so
the check must reject a nonempty list.

## Declare files once, and keep lint out of downstream builds

A [Bazel glob](https://bazel.build/reference/be/functions#glob) stops at
subpackage boundaries; traversing build targets misses unreferenced
files. Declare first-party inputs once, retain their relative paths, and
give each check only the files and configs it reads. A new BUILD.bazel,
an unreferenced header or a Markdown file in a new package must be found
without adding a lint rule to that package. Exclude nested foreign
repositories and build outputs. File additions, removals and config
edits must invalidate the appropriate check. No remote action should
scan a checkout.

Use [dev-only declarations](https://bazel.build/rules/lib/globals/module#bazel_dep)
for lint-only modules, extension usages, repo declarations and toolchain
registrations as applicable. Isolate their Starlark loads in the lint
package. Do not mark the shared Go, Python, QEMU or LLVM toolchain
development-only merely because a check also uses it.

The [two-module comparison](https://github.com/tamird/gvisor/actions/runs/40905219042)
shows why both halves matter. Marking the wheel dev-only broke
`@gvisor//:license`: the root BUILD still loaded codespell’s requirements.
Isolating that load fixed the root license target under
`--ignore_dev_dependency`. An [external Go build on unmodified master](https://app.buildbuddy.io/invocation/a7df6eb9-a593-4a9e-93cc-be4780e8739a)
and the [isolated proposal](https://app.buildbuddy.io/invocation/b294a411-549f-4a42-98e5-f96236fc99da)
both still fail in Gazelle. gVisor [declares overrides](https://github.com/google/gvisor/blob/0fa6803f2/MODULE.bazel#L830-L849)
that [Gazelle rejects in a non-root, non-isolated module](https://github.com/bazel-contrib/bazel-gazelle/blob/b66b19ce4/internal/bzlmod/go_deps.bzl#L81-L94).
Moving the Go overrides into a dev-only extension hides that error but
also changes what gVisor builds. In a [two-module comparison](https://github.com/tamird/gvisor/actions/runs/40906632925),
the ordinary gVisor root and an external root that explicitly applied the
overrides both got the patched protobuf source. A plain consumer and a
gVisor build with dev dependencies disabled both got the upstream source
that reads the executable at startup. [Removing that read was intentional](https://github.com/google/gvisor/commit/dba564686).
The comparison verified source contents; both attempts to compile
`waiter` stopped during Go toolchain setup. Keep production Go overrides
out of the lint migration. Proving that gVisor builds as a dependency
requires a separate fix.

## Migration order

1. Hold [#16663](https://github.com/google/gvisor/pull/16663). Prove
   Buildifier and cpplint share file discovery, fail independently on
   bad inputs, run without host tools, and do not leak lint-only
   dependencies into a consuming module’s repository mapping.
2. Migrate clang-format, goimports and codespell to the same interface.
   Preserve useful controls: [485 C/C++ files on scratch](https://app.buildbuddy.io/invocation/a98b6745-cb67-4e80-942a-1b9d286a60d3),
   a [new-package violation](https://app.buildbuddy.io/invocation/5b88fc8b-dd38-400a-a493-4ede1ba9c0d7),
   [3,283 spelling inputs](https://app.buildbuddy.io/invocation/234918c9-c55a-475f-bb3c-a4ef43fb4ca4)
   and a [config-only change](https://app.buildbuddy.io/invocation/92e3db8a-8ee3-4809-b065-fd863587e429).
3. Switch CI and developer callers and delete [the script](https://github.com/google/gvisor/blob/0fa6803f2/tools/lint.sh)
   once check and fix coverage match. Keep Bazelisk bootstrap separate:
   it selects Bazel; it does not decide what to lint.
