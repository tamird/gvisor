# How gVisor should run lint

**Delete `tools/lint.sh`.** Give contributors and CI one read-only
command, `bazel build --keep_going //tools/lint:check`. Give contributors
one opt-in writing command, `bazel run //tools/lint:format`. These are
proposed interfaces. A temporary `make lint` alias can call Bazel but
should own no policy.

There are two kinds of lint input. Buildifier and cpplint need a list of
source files and their configuration. They can run together under
`//tools/lint:check`. Clang-tidy needs the compiler flags and generated
headers for each C++ target. The proposed Bazel aspect runs it once per
configured compilation. Developers can still generate
`compile_commands.json` for their editors.
[Source lint PR](https://github.com/google/gvisor/pull/16663) ·
[Clang-tidy aspect](https://github.com/tamird/gvisor/commit/89a85fa8f)

| Check | Where it belongs |
| --- | --- |
| Buildifier, goimports, clang-format | Independent source actions. The format command uses the same files, tools, configs and options as the checks. |
| cpplint, codespell | Read-only source actions with their declared Python interpreter, package-local lint configs and spelling dictionary. No automatic spelling edits. |
| Gazelle | Check generated Go rules with [`bazel build //:gazelle_check`](https://github.com/tamird/gvisor/compare/3c2172c05...0eb8eca5b). A stale rule should print the proposed diff; a malformed BUILD file should print Gazelle’s parse error. Keep the packages and flags shared with `gazelle_fix` so applying the fix clears the check. |
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

The Buildifier check uses Bazel’s selected native tool and declares the
files it reads. The cpplint action runs the declared Python interpreter.
For goimports, a list of misformatted files on stdout must fail the
check even when the tool exits zero.
[Lint targets](https://github.com/tamird/gvisor/blob/54a734f17/tools/lint/BUILD)

## Run the supplier’s native formatter

The Buildifier check needs a binary for the remote worker.
`bazel run //tools/lint:format` needs one for the developer’s machine.
The same checkout can use both, for example when a macOS client sends
checks to Linux. Each rule selects the toolchain for the machine that
will execute the binary.
[Check and formatter rules](https://github.com/tamird/gvisor/blob/54a734f17/tools/lint/defs.bzl)

The upstream toolchain exposes the executable as `tool`. gVisor’s
pinned 8.5.1.4 release predates that field, so #16663 backports the
one-line declaration. Drop the patch when the pinned release contains
the upstream commit.
[Backport](https://github.com/tamird/gvisor/blob/54a734f17/tools/lint/buildifier_native.patch) ·
[Upstream commit](https://github.com/keith/buildifier-prebuilt/commit/9cefc248d)

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
A consuming root that explicitly adopts gVisor’s Go overrides built
`@gvisor//pkg/gvisordetect` on [unmodified gVisor](https://github.com/tamird/gvisor/actions/runs/41220013696/artifacts/13050115599)
and [the lint amendment](https://github.com/tamird/gvisor/actions/runs/41220624411/artifacts/13049921773).
The Go archive and export data matched byte for byte. The amendment
kept the patched protobuf source and added none of its three lint
repositories to the consumer’s gVisor mapping or registry lock. A root
that doesn't adopt that dependency policy still encounters the existing
Gazelle restriction. Changing that policy is separate from source lint.

## Migration order

1. Move Buildifier and cpplint under `//tools/lint:check`. While Make
   starts lint, pass its Bazel options through to every call, including
   `info` and `cquery`. The amendment uses the helper in Ryan’s PR.
   [#16663](https://github.com/google/gvisor/pull/16663) ·
   [Amendment after #17612](https://github.com/tamird/gvisor/commit/8fd9f92fc)
2. Migrate clang-format, goimports and codespell to the same interface.
   Preserve useful controls: [485 C/C++ files on scratch](https://app.buildbuddy.io/invocation/a98b6745-cb67-4e80-942a-1b9d286a60d3),
   a [new-package violation](https://app.buildbuddy.io/invocation/5b88fc8b-dd38-400a-a493-4ede1ba9c0d7),
   [3,283 spelling inputs](https://app.buildbuddy.io/invocation/234918c9-c55a-475f-bb3c-a4ef43fb4ca4)
   and a [config-only change](https://app.buildbuddy.io/invocation/92e3db8a-8ee3-4809-b065-fd863587e429).
3. Before switching Buildkite from Make to Bazel, compare the effective
   cache settings on a first-pass agent and a regular agent. First-pass
   must read its configured cache with uploads disabled. The regular job
   must keep its agent’s existing settings. Check the actual commands,
   including the ARM64 build in the source presubmit.
   [Ryan’s requirement](https://github.com/google/gvisor/pull/17612#issuecomment-6858919995)

   The agent’s post-checkout hook supplies `BAZEL_REMOTE_CACHE` to Make.
   For direct Bazel commands the same hook can export an agent-owned rc
   file through `BAZELRC`. A local Bazel 9.3.1 test populated an HTTP cache
   with regular builds. With first-pass uploads disabled, both `build`
   and `test` read cached results and executed cold misses with zero PUTs.
   Later command-line options can override the rc settings.
   [Agent hook](https://github.com/google/gvisor/commit/8dbabb771) ·
   [Bazel’s rc loading order](https://github.com/bazelbuild/bazel/blob/fee2bf2d1/src/main/cpp/option_processor.cc#L411-L429)

   For remote execution, verify the endpoint and write policy with the
   agent owner. Bazel rejects combining an HTTP cache with a gRPC
   executor. The first-pass client flag controls uploads of locally
   executed results. [Protocol check](https://github.com/bazelbuild/bazel/blob/fee2bf2d1/src/main/java/com/google/devtools/build/lib/remote/RemoteModule.java#L534-L538)
4. Delete `tools/lint.sh` when the direct check and format targets cover
   the existing callers. Bazelisk selects the Bazel version.
