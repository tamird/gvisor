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

The differ compares the ioctls used by a supported base driver with their
definitions in a second driver. It reads the same pinned NVIDIA source
archives as the ABI test. To try a release before adding it to nvproxy's
version tree, include its version when updating the pins:

```bash
bazel run //tools/nvidia_driver_differ/sources:update -- 620.70.06
bazel mod tidy
bazel run //tools/nvidia_driver_differ:run_differ -- --base 615.71.09 --next 620.70.06
```

For two already pinned releases, only the last command is needed. Bazel
provides the headers as runfiles. The differ reports changes to the
definitions it finds. These differences can be additions of new fields to
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

## Keeping the source pins current

After changing the versions supported by nvproxy, run
`bazel run //tools/nvidia_driver_differ/sources:update` to record its
public source commit and archive checksum. Run `bazel mod tidy` to
import the archives into the root module. Running the updater without a
candidate drops trial versions outside the supported set. If NVIDIA has not published
a matching tag, the ABI test reports that version as skipped.

Run `//pkg/sentry/devices/nvproxy:nvproxy_driver_abi_test` to compare the
native driver definitions with the Go types. The separate
`//pkg/sentry/devices/nvproxy:nvproxy_driver_checksums_test` downloads the
installers to verify their published checksums.
