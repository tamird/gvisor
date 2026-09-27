# NVIDIA Driver Differ

This tool is intended to help adoption of new NVIDIA driver versions. It
compares two driver versions (one currently supported by `nvproxy` and one that
is not) and reports any changes to `ioctl` structs that exists between the two
versions. However, it only does this for `ioctl` structs that are currently
supported by `nvproxy`, giving a targeted but comprehensive understanding of
what changes need to be reflected in `nvproxy` to support the newer driver
version.

To do this, the tool needs to parse the NVIDIA driver source code. This is done
using
[Clang's AST Matcher API](https://clang.llvm.org/docs/LibASTMatchersReference.html)
to generate an AST of the NVIDIA driver, which the tool then searches and
traverses to get a comprehensive definition of every struct `nvproxy` relies on.

## Usage

Everything is packaged for convenience inside `run_differ`. The differ accepts
two version numbers as arguments: a `base` version which is currently supported
in `nvproxy`, and a `next` version that is compared against `base`. For example,
comparing versions `550.90.07` and `560.31.02` can be done like so:

```bash
make run TARGETS=//tools/nvidia_driver_differ:run_differ ARGS="--base 550.90.07 --next 560.31.02"
```

This will fetch the corresponding source code from Github, parse it using Clang,
and then compare the definitions that were found. Any differences will be
printed to standard output. These differences can be additions of new fields to
structs, or changes to the name or type of existing fields. For example:

```
struct NEW_FIELD_EXAMPLE
  []parser.RecordField{
    ... // 10 identical elements
+   s"bool newField1"
+   s"int newField2"
  }

struct MODIFIED_FIELD_EXAMPLE
  []parser.RecordField{
    ... // 10 identical elements
    {
-     Name:   "oldName",
+     Name:   "newName",
      Type:   "int",
      Offset: 36
    },
    {
      Name:   "arrayField",
-     Type:   "int[10]",
+     Type:   "int[12]",
      Offset: 40
    },
  }
```

[A deeper dive into how this tool works can be found here.](https://github.com/google/gvisor/blob/master/g3doc/proposals/nvidia_driver_differ.md)

## Updating pinned driver sources

The ABI parity test reads JSON produced by
`//tools/nvidia_driver_differ:driver_abi`. Each extraction action uses a pinned
driver archive and the selected target C++ toolchain's flags, sysroot and Clang
resource headers. The parser runs on the execution platform; the definitions
describe the target architecture. Tests need neither Git nor system headers.
The standalone `run_differ` command continues to fetch the versions it compares.

`//tools/nvidia_driver_differ/sources:update` derives the public source pins from
`nvproxy.SupportedDrivers()`. Run this Bazel target after changing the supported
driver registry. It updates its marked block in `MODULE.bazel` and generates
`driver_sources.bzl`; neither output has a separate hand-maintained version list.
The `--workspace` argument selects the checkout to update and defaults to Bazel's
`BUILD_WORKSPACE_DIRECTORY`.

The updater uses Git to list all public tags in one successful request, then
resolves annotated tags to their commits and hashes the source archives. Only
versions absent from that advertisement are recorded as unavailable; repository
access failures and archive download errors stop the update. Git is needed only
by this update command and the standalone differ, not by ABI extraction actions.
The parity test requires the generated index to cover the complete supported
version registry. Versions with no public source tag appear as explicit skipped
subtests; missing index entries and extraction failures fail the test or build.
Review the generated changes and regenerate the canonical dependency license
inventory before submitting them.

The separate `nvproxy_driver_checksums_test` still checks live upstream installer
downloads without test-result caching. The `nvproxy_driver_parity_test` suite
selects both ABI and installer checks.
