# Explicit runtime inputs for test images

The user approved PIDFD4be5 and asked to use scratch wherever possible,
across all images. Treat every runtime command, library, certificate and
filesystem entry as an explicit image input. A digest-pinned distribution
image is reproducible, but its inherited executable payload is still
prebuilt and obscures the source-built qualification boundary.

## Existing source definitions

The active-head inventory and deduplicated image manifests are retained in
`active-image-head-inventory.json` and `image-base-definitions.json`.
`//images:helper_base` already has no parent image: only declared CA
certificates, a sticky /tmp directory, PATH/SSL metadata and numeric root.
Native helpers, integration C, hackbench, hey, iperf, wrk2, ACL, filecap and
sudo use it. Some add declared prebuilt distro packages; this is explicit
packaging, not a claim those package binaries were built from source.

## Remaining inherited bases

| Image | Inherited base | Observed runtime requirement |
| --- | --- | --- |
| syscallbench | BusyBox; scratch replacement awaiting validation | sleep 24h keeps the container alive for repeated execs |
| tmpfile | Alpine; scratch replacement awaiting validation | cat reads the mounted /tmp fixture |
| symlink-resolv | BusyBox | sleep, readlink and cat exercise containerd resolver setup |
| tcpdump | BusyBox | sleep infinity; sh redirects capture output to container logs |
| fsstress | BusyBox | sh remains parent so workers do not see parent PID 1 |
| basic/alpine, basic/busybox | Their named upstream images | Shared fixture consumers require a wider command inventory before changing payloads |

The proposed replacement starts from scratch and adds the required
executable inputs and paths explicitly. Preserve command, timeout, process
parent and filesystem behavior while changing their source of supply.

## Shared fixture compatibility

The indexed consumer audit uses basic-image source1d562c448. In addition to
commands, the replacement must preserve these concrete runtime contracts:

- `test/e2e/exec_test.go`: TTY job control for `sleep | cat`; account creation
  using BusyBox-compatible `adduser`/`addgroup` options; `bin` has home `/bin`,
  root has `/root`, and dynamically created users retain their chosen home.
- `test/e2e/integration_test.go`: `nobody` account, shell redirection and
  pipelines, fractional sleeps, pgrep, ip, dd, head, expr, mount utilities,
  plus filesystem and stderr behavior consumed by existing assertions.
- `test/root/crictl_test.go`: runtime-derived HOME, `setsid`, process grouping
  and long-lived sleep. Do not set HOME in image metadata to mask that test.
- `test/root/crictl_emptydir_test.go`: grep, df, dd and stat; the test checks
  actual tmpfs size, ENOSPC output and final file size.

This is a source inventory, not runtime qualification or an exhaustive
proof of utility compatibility. Retain existing consumers as validation.

## Qualified PIDFD replacement

The [PIDFD image](https://github.com/tamird/gvisor/commit/4217084f534a8e0df109de9961630f24d3386d10)
starts from scratch. It builds BusyBox 1.38.0 with a limited allnoconfig
fragment and installs fourteen explicit applet paths. Its shell supports
the runner's pipefail behavior, and tr supports the required character
classes. GNU timeout is built from Coreutils 9.11 and linked statically
against musl; it supplies the exit-status and process-group behavior used
by the PIDFD runner. Both programs carry their license notices.

Both architecture image builds and their link-closure checks pass. All seven
PIDFD programs pass in the existing AMD64 runtime checks; one unsupported
kcmp case remains skipped within a program. The full license audit passes.
ARM64 runtime execution is not covered by these results. The published
commit has the same source tree as the qualified 592a453393 revision.

The result records are `pidfd-scratch-final-root-acceptance.json`,
`qualification-pidfd-scratch-runtime-retry2-outcome.json`, and
`qualification-pidfd-scratch-licenses-retry1-outcome.json`. They retain the
build, runtime and license boundaries rather than treating removal of the
parent image as proof of complete source closure.

Build inputs select target ARCH and CC/AR/NM separately from execution HOSTCC.
Kconfig timestamps are disabled. The pinned RBE execution image still
supplies build shell utilities. Other image profiles also retain declared
prebuilt packages for mkdir/chmod/rm and some shared libraries; those inputs
remain part of the source-build audit.

## Next replacements

The [tmpfile and syscallbench change](https://github.com/tamird/gvisor/commit/6cae99aa007bf7b1f87e5fe0a8f047be228f7e03)
reuses the source-built BusyBox binary. Tmpfile installs only cat's applet
path alongside its payload and sticky /tmp directory. Syscallbench installs
sleep's path and enables duration suffixes for its existing sleep 24h
command. Both images start from scratch and include the executable's
notices. Independent source review has passed; remote consumer validation
is pending. BusyBox, tmpfile and PIDFD cover both target architectures.
Syscallbench retains its existing AMD64-only syscall assembly and ABI
constraint, so its build and runtime validation remain AMD64-only.

Retain the existing tests as acceptance checks: TestTmpFile must read the
fixture, and all four syscallbench modes must run through their long-lived
container. Recheck PIDFD consumers when changing the shared applet build.
The remaining resolver, tcpdump, fsstress and shared fixture bases need
individual command inventories and their existing consumer tests.

Prefer reusable source executables with explicit per-image installed paths.
Do not replace one implicit default utility image with another. Resource
file relocations are independent and do not change runtime bases. Existing
scratch-based changes retain their contributor approvals.
