# How gVisor should run lint

**Delete `tools/lint.sh`.** Give contributors and CI one read-only
command, `bazel build --keep_going //tools/lint:check`. Give contributors
one opt-in writing command, `bazel run //tools/lint:format`. These are
proposed interfaces. A temporary `make lint` alias can call Bazel but
should own no policy.

On the [working branch](https://github.com/tamird/gvisor/commit/378946ccd),
the check runs Buildifier and cpplint independently. On an
[AMD64 scratch worker](https://app.buildbuddy.io/invocation/1c7ef0fa-8529-4c89-9410-71be0a31c57a)
they checked 617 Bazel files and 485 C++ files using the registered
tools. A [new C++ file with a bad include](https://app.buildbuddy.io/invocation/c118d0f9-150a-44dc-8c5b-d3c177052d09)
failed cpplint. These results cover running the checks; bootstrapping
all their tools on scratch is still open.

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

## Run the supplier’s native formatter

Keep the released Buildifier. The BCR module
[buildifier_prebuilt](https://bcr.bazel.build/modules/buildifier_prebuilt/8.5.1.4/source.json)
already selects the native binary for the execution platform, but its
[public executable](https://github.com/keith/buildifier-prebuilt/blob/2d23069a6/buildifier/buildifier_binary.bzl#L8-L29)
runs a Bash or Batch wrapper. The only public raw binaries have
platform-specific repository names. A [nine-line supplier change](https://github.com/tamird/gvisor/blob/ea7ed498a/tools/lint/buildifier_native.patch)
exposes the selected executable as `@buildifier_prebuilt//buildifier:native`.
The released tool checked 617 files and rejected a new malformed file
on shell-free [Linux AMD64](https://github.com/tamird/gvisor/actions/runs/40917863627)
and [Linux ARM64](https://github.com/tamird/gvisor/actions/runs/40919147278)
workers. The interactive CLI keeps its working-directory behavior.

Building Buildifier from Go source would add a second bootstrap: the
[Go toolchain requires Bash](https://app.buildbuddy.io/invocation/596d4698-e775-4376-9bff-fe246139cb86).
With Go compilation on Ubuntu and only lint execution on scratch, both
the Gazelle-selected revision and the exact 8.5.1 release
[accepted the checkout](https://app.buildbuddy.io/invocation/48d9c263-662d-4bd2-bed6-35448c450a43),
[rejected a new malformed file](https://app.buildbuddy.io/invocation/aca9afed-9b64-42f6-a502-8239eb90a09d)
and distinguished [valid](https://app.buildbuddy.io/invocation/945493a0-df07-493d-86ec-f8d92c6590c7)
from [invalid configuration](https://app.buildbuddy.io/invocation/df00c424-5308-4112-a8b8-5f40d7b8857d).
That establishes parity for these inputs, not a reason to downgrade the
formatter. Keep the supplier and the lint inputs dev-only.

Use Buildifier's filename diagnostic in CI. Its diff mode invokes
`/usr/bin/env`, Bash and `diff`; its check mode [names the same bad file
without those programs](https://github.com/tamird/gvisor/actions/runs/40931472870).
For edits, the [format target](https://github.com/tamird/gvisor/actions/runs/40927701416)
uses the check's file list and configuration, including when invoked
from a subdirectory. Running it twice leaves already-formatted files
untouched. The formatter can run in an empty root; the
[Bazel 8.8.1 Linux client](https://github.com/bazelbuild/bazel/blob/f8278f94e/src/main/java/com/google/devtools/build/lib/runtime/commands/RunCommandLine.java#L158-L176)
still starts `bazel run` targets through `sh -c`.

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

1. [#16663](https://github.com/google/gvisor/pull/16663) can use the
   shared file list and the supplier's native executable. The switch
   still needs a released native target or the small supplier patch
   above. The [integrated license check](https://github.com/tamird/gvisor/actions/runs/40931472870)
   verified 222 records.
2. Migrate clang-format, goimports and codespell to the same interface.
   Preserve useful controls: [485 C/C++ files on scratch](https://app.buildbuddy.io/invocation/a98b6745-cb67-4e80-942a-1b9d286a60d3),
   a [new-package violation](https://app.buildbuddy.io/invocation/5b88fc8b-dd38-400a-a493-4ede1ba9c0d7),
   [3,283 spelling inputs](https://app.buildbuddy.io/invocation/234918c9-c55a-475f-bb3c-a4ef43fb4ca4)
   and a [config-only change](https://app.buildbuddy.io/invocation/92e3db8a-8ee3-4809-b065-fd863587e429).
3. Switch CI and developer callers and delete [the script](https://github.com/google/gvisor/blob/0fa6803f2/tools/lint.sh)
   once check and fix coverage match. Keep Bazelisk bootstrap separate:
   it selects Bazel; it does not decide what to lint.
