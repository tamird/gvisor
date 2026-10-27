# Branch `state-rewrite`: a faster `pkg/state` (gvisor#16216)

This branch rewrites `pkg/state`, the serializer used for checkpoint and
restore, to make saving and loading the sentry's object graph much cheaper. It
is based on upstream commit `d88e105cf`. See
[google/gvisor#16216](https://github.com/google/gvisor/issues/16216).

The checkpoint format is not compatible with earlier versions: a state file can
only be restored by a binary with the same registered types. The stream starts
with a schema fingerprint, and Load fails cleanly on a mismatch.

## Results

All results are from `//test/benchmarks/checkpointfile`, using `benchstat`.
The benchmark checkpoints an idle nginx container with 4 workers, then decodes
and re-encodes the kernel in-process. Each tree checkpoints with its own
`runsc`.

Two modes were measured:

*   **Warm**: the benchmark as written, many iterations per process.
*   **Cold**: one iteration per process. This includes the one-time cost of
    building per-type information, as in a real restore.

The `Statefile` variants read and write through the real `statefile` reader and
writer, as `runsc` does.

The measurements were taken on a shared 16-vCPU Intel Xeon @ 2.20GHz Linux VM,
with 16 interleaved runs per tree (24 for the cold comparison with `ar/scg3`).

### Against the legacy implementation (upstream `d88e105cf`)

| Mode | Benchmark | Legacy | This branch | Change |
|------|-----------|-------:|------------:|-------:|
| Warm | Decode time | 21.43 ms | 2.73 ms | **−87.3%** |
| Warm | Encode time | 24.77 ms | 3.01 ms | **−87.9%** |
| Cold | Decode time | 26.04 ms | 7.64 ms | **−70.7%** |
| Cold | Encode time | 24.33 ms | 3.86 ms | **−84.2%** |
| Warm | Decode allocs/op | 91.2k | 3.39k | **−96.3%** |
| Warm | Encode allocs/op | 77.4k | 1.28k | **−98.3%** |
| Warm | Decode B/op | 4.57 MiB | 1.64 MiB | −64.0% |
| Warm | Encode B/op | 5.71 MiB | 1.81 MiB | −68.3% |
| | State file | 296.9 KiB | 146.3 KiB | **−50.7%** |

### Against the codegen prototype ([`EtiennePerot/gvisor@ar/scg3`](https://github.com/EtiennePerot/gvisor/tree/ar/scg3))

`ar/scg3` was applied to the same base and benchmarked with the same harness on
the same machine. It generates typed `StateEncode`/`StateDecode` methods and
uses a compact typed stream.

| Benchmark | ar/scg3 (warm) | This branch (warm) | Change | ar/scg3 (cold) | This branch (cold) | Change |
|-----------|---------------:|-------------------:|-------:|---------------:|-------------------:|-------:|
| Decode | 4.65 ms | 2.72 ms | **−41.5%** | 8.48 ms | 7.85 ms | **−7.5%** |
| Encode | 5.38 ms | 3.03 ms | **−43.7%** | 5.98 ms | 3.58 ms | **−40.2%** |
| DecodeStatefile | 4.54 ms | 2.51 ms | **−44.7%** | 4.74 ms | 2.69 ms | **−43.3%** |
| EncodeStatefile | 5.56 ms | 3.49 ms | **−37.1%** | 6.07 ms | 3.97 ms | **−34.7%** |
| Decode allocs/op | 7.90k | 3.39k | −57.1% | 8.31k | 5.53k | −33.4% |
| Encode allocs/op | 1.86k | 1.27k | −31.7% | 1.88k | 1.28k | −31.7% |
| State file | 161.3 KiB | 146.3 KiB | −9.3% | | | |

All differences above are significant (p ≤ 0.001). One result goes the other
way: the warm `EncodeStatefile` allocates 18% more bytes than `ar/scg3` (2.56
vs 2.16 MiB/op).

## The new approach

### Reflection once per type

Each Go type gets a `typeInfo`, built once with reflection
(`typeinfo_unsafe.go`). Values are then saved and loaded by switching on the
kind and working on `unsafe.Pointer`s:

*   The generated `StateSave`/`StateLoad` methods still pass `&x.field` as
    `any`. The interface's type word is checked against a per-struct,
    per-slot cache (one atomic load and a compare).
*   Fields of predeclared types use typed methods (`SaveInt64`/`LoadInt64`,
    etc.) emitted by `go_stateify`.
*   Arrays of plain data are bulk-copied. Structs registered with
    `RegisterNoHooks` whose fields are all plain data are saved as their
    memory representation.
*   Struct methods are called through interface tables (itabs). `go_stateify`
    registers structs with `RegisterStruct`/`RegisterNoHooks(StructType)`, so
    the `(*T, StructType)` itab is emitted by the compiler, not built at run
    time.

### Objects and targets (encoding)

The object graph is split into *objects*: single values, the backing arrays of
slices, and maps. A pointer is written as a *target index*, and the target
table maps each index to an object and a byte offset.

*   New pointers are first looked up by exact address. Misses go to a block
    index, which finds objects whose address ranges overlap. Both use a custom
    open-addressing hash table (`addrmap.go`).
*   An object found later that contains earlier objects *absorbs* them: their
    targets move into the container, and their finished encodings are spliced
    into it. So `StateSave` runs exactly once per value, and interior pointers,
    shared slice windows and aliasing are preserved.
*   Of arrays, only the elements within some slice, or pointed to, are saved.
    These are tracked as covered ranges and encoded as chunks.
*   The whole stream is built in memory and written with a single `Write`.

The stream holds, in order:

1.  A magic number and the schema fingerprint.
2.  The type table.
3.  The object table, with the kind, type and length of each object, including
    the entry count of maps.
4.  The target table.
5.  The data of each object.

### Decoding

Load reads the stream with a single `ReadFull`, allocates every object
upfront, and resolves every target to a pointer. It then decodes each object
in one pass, with no intermediate tree and no `FieldByName`:

*   Maps are allocated at their final size.
*   Map key and value temporaries are reused, unless code that may keep their
    address ran while decoding the entry.
*   Objects whose types include unregistered named interfaces or maps are
    decoded once a reference reveals their exact static type.
*   Load callbacks (`afterLoad`, `wait` fields, values loaded through
    `state:".(T)"`) run in dependency order, with cycle detection.
    `go_stateify` now generates `StateAfterLoad`/`StateLoadValue` methods, so
    scheduling these callbacks does not allocate closures. Values loaded
    through `.(T)` fields are decoded into reused storage when they are passed
    on at once.

### Cold start

A restore is a single Load, so the one-time cost of building `typeInfo`s
matters. Several changes keep it low:

*   `typeInfo`s, slot caches and slot entries come from slabs that are never
    freed.
*   Lookups go through a map under an RWMutex, plus per-stream direct-mapped
    caches for interface types and itabs. This replaces `sync.Map`.
*   `reflect.SliceOf` is called lazily, and no zero values are allocated to
    classify types.
*   Static itabs are used where possible (see above).

### Sentry-side changes

Some of these come from ideas in
[this comment](https://github.com/google/gvisor/issues/16216#issuecomment-6429916909)
and `ar/scg3`:

*   `vfs.FileLocks` allocates its lock sets on first use.
*   The cpuid static function is saved as packed bytes.
*   The unused IPv4 IP ID state is removed.
*   Per-type stats are opt-in (`state.CollectStats`).

### Correctness

Tests were added before the engine was changed, and are used as the gate:

*   `pkg/state/tests/isomorphism_test.go`: a graph-isomorphism checker (shared
    pointers, interior pointers, slice windows, map identity), wired into
    every existing test case.
*   `pkg/state/tests/graph_test.go`: randomized object graphs and targeted
    tests, including truncation at every byte.
*   The save/restore syscall tests, netstack and kernfs save tests, and a kernel
    round trip in `checkpointfile` (`TestCheckpointFileRoundTrip`) also pass.

These tests found two bugs in the legacy encoder, both involving slices that
share a backing array:

*   Save cleared `s[len:cap]` of live slices.
*   Sub-slices at a non-zero offset failed.

### Behavior changes

*   Restores require a binary with the same registered types. The pretty
    printer (`runsc statefile`) needs a binary that links the saved types.
*   Elements of backing arrays past `len` that are in no slice and not pointed
    to are not saved, and load as zero.
*   Values of unregistered named types (e.g. `type X []int`) can't be saved in
    interfaces, since Load could not restore their type.

### Running the benchmark without Docker

`checkpointfile_test` accepts `--checkpoint_dir=<dir>`. The directory must
hold the files of a `runsc checkpoint` and a `spec.json`, which lists the
container names that `runsc` may use and the destinations of the container's
root and mounts, as `{"names": [...], "mounts": [...]}`. For example:

```
checkpointfile_test --checkpoint_dir=/path/to/ckpt -test.bench=CheckpointFile -test.run=XXX
```

---

![gVisor](g3doc/logo.png)

[![Build status](https://badge.buildkite.com/3b159f20b9830461a71112566c4171c0bdfd2f980a8e4c0ae6.svg?branch=master)](https://buildkite.com/gvisor/pipeline)
[![Issue reviver](https://github.com/google/gvisor/actions/workflows/issue_reviver.yml/badge.svg)](https://github.com/google/gvisor/actions/workflows/issue_reviver.yml)
[![CodeQL](https://github.com/google/gvisor/actions/workflows/codeql.yml/badge.svg)](https://github.com/google/gvisor/actions/workflows/codeql.yml)
[![code search](https://img.shields.io/badge/code-search-blue)](https://cs.opensource.google/gvisor/gvisor)

## What is gVisor?

**gVisor** provides a strong layer of isolation between running applications and
the host operating system. It is an application kernel that implements a
[Linux-like interface][linux]. Unlike Linux, it is written in a memory-safe
language (Go) and runs in userspace.

gVisor includes an [Open Container Initiative (OCI)][oci] runtime called `runsc`
that makes it easy to work with existing container tooling. The `runsc` runtime
integrates with Docker and Kubernetes, making it simple to run sandboxed
containers.

## What **isn't** gVisor?

*   gVisor is **not a syscall filter** (e.g. `seccomp-bpf`), nor a wrapper over
    Linux isolation primitives (e.g. `firejail`, AppArmor, etc.).
*   gVisor is also **not a VM** in the everyday sense of the term (e.g.
    VirtualBox, QEMU).

**gVisor takes a distinct third approach**, providing many security benefits of
VMs while maintaining the lower resource footprint, fast startup, and
flexibility of regular userspace applications.

## Why does gVisor exist?

Containers are not a [**sandbox**][sandbox]. While containers have
revolutionized how we develop, package, and deploy applications, using them to
run untrusted or potentially malicious code without additional isolation is not
a good idea. While using a single, shared kernel allows for efficiency and
performance gains, it also means that container escape is possible with a single
vulnerability.

gVisor is an application kernel for containers. It limits the host kernel
surface accessible to the application while still giving the application access
to all the features it expects. Unlike most kernels, gVisor does not assume or
require a fixed set of physical resources; instead, it leverages existing host
kernel functionality and runs as a normal process. In other words, gVisor
implements Linux by way of Linux.

gVisor should not be confused with technologies and tools to harden containers
against external threats, provide additional integrity checks, or limit the
scope of access for a service. One should always be careful about what data is
made available to a container.

## Documentation

User documentation and technical architecture, including quick start guides, can
be found at [gvisor.dev][gvisor-dev].

## Installing from source

gVisor builds on x86_64 and ARM64. Other architectures may become available in
the future.

For the purposes of these instructions, [bazel][bazel] and other build
dependencies are wrapped in a build container. It is possible to use
[bazel][bazel] directly, or type `make help` for standard targets.

### Requirements

Make sure the following dependencies are installed:

*   Linux 5.6+
*   [Docker version 17.09.0 or greater][docker]

### Building

Build a release tarball containing `runsc`, the `containerd-shim-runsc-v1`
containerd shim, and a few sidecar binaries that `runsc` expects to find in a
`gvisor-bin/` directory next to itself, then extract it to `/usr/local/bin`:

```sh
make release-tarball DESTINATION=bin/
sudo tar -C /usr/local/bin -xf bin/gvisor.tar.bz2
```

To build specific libraries or binaries, you can specify the target:

```sh
make build TARGETS="//pkg/tcpip:tcpip"
```

### Building directly with Bazel (without Docker)

Using Bazel directly isn't recommended due to the extra overhead, but in order
to get started:

-   Look at the [build dockerfile](images/default/Dockerfile) for the canonical
    list of needed dependencies.
-   Install and use [bazelisk][bazelisk]. Otherwise, make sure your bazel
    version matches the one listed in the [.bazelversion](.bazelversion) file.

After setting up dependencies, using Bazel is similar to the Makefile:

```sh
bazel build -c opt //debian:gvisor-release-tar-bz2
```

### Testing

To run standard test suites, you can use:

```sh
make unit-tests
make tests
```

To run specific tests, you can specify the target:

```sh
# Makefile
make test TARGETS="//runsc:version_test"
# Bazel
bazel test //runsc:version_test
```

### Mac OS

Some packages support running tests directly on macOS. At the time of this
writing, gVisor requires bazel 8, which you can install via homebrew:

```sh
brew install bazel@8

# You can then run the tests, e.g.:
$(brew --prefix bazel@8)/bin/bazel test --macos_sdk_version=$(xcrun --show-sdk-version) -- //tools/nogo/... //tools/check{aligned,const,escape,linkname,locks,unsafe}/...
```

### Using `go get`

This project uses [bazel][bazel] to build and manage dependencies. A synthetic
`go` branch is maintained that is compatible with standard `go` tooling for
convenience. This is useful for external packages and libraries that depend on
gVisor subpackages (e.g. userspace networking via Netstack) to import gVisor Go
code into their Go projects.

Select this branch explicitly with the `go` branch query. `@latest` resolves
`master`, which requires Bazel and is not compatible with standard Go tooling:

```sh
go get gvisor.dev/gvisor/pkg/tcpip/transport/tcp@go
```

**NOTE**: **`runsc` builds from this branch are not supported**. gVisor and
`runsc` require several binaries (some of which are not even written in Go) in
order to function. The `go` branch is supported in a best effort capacity, and
direct development on this branch is not supported. Development should occur on
the `master` branch, which is then reflected into the `go` branch.

## Community & Governance

See [GOVERNANCE.md](GOVERNANCE.md) for project governance information.

See [ADOPTERS.md](ADOPTERS.md) for a list of known production users and
adopters.

The [gvisor-users mailing list][gvisor-users-list] and
[gvisor-dev mailing list][gvisor-dev-list] are good starting points for
questions and discussion.

## Security Policy

See [SECURITY.md](SECURITY.md).

## Releases

See [Releases](g3doc/release.md).

## Contributing

See [Contributing.md](CONTRIBUTING.md).

[bazel]: https://bazel.build
[docker]: https://www.docker.com
[gvisor-users-list]: https://groups.google.com/forum/#!forum/gvisor-users
[gvisor-dev]: https://gvisor.dev
[gvisor-dev-list]: https://groups.google.com/forum/#!forum/gvisor-dev
[linux]: https://en.wikipedia.org/wiki/Linux_kernel_interfaces
[oci]: https://www.opencontainers.org
[sandbox]: https://en.wikipedia.org/wiki/Sandbox_(computer_security)
[bazelisk]: https://github.com/bazelbuild/bazelisk
