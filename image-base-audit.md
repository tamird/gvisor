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
| syscallbench | BusyBox | sleep 24h keeps the container alive for repeated execs |
| tmpfile | Alpine | cat reads the mounted /tmp fixture |
| symlink-resolv | BusyBox | sleep, readlink and cat exercise containerd resolver setup |
| tcpdump | BusyBox | sleep infinity; sh redirects capture output to container logs |
| fsstress | BusyBox | sh remains parent so workers do not see parent PID 1 |
| PIDFD | BusyBox | sh, thirteen utility applets, GNU timeout; the C test itself execs /bin/sleep |
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

## First implementation

Hostinet owns the PIDFD prototype on `rules-img-pidfd-scratch`, preserving
approved4be5. BCR Toybox0.8.12.bcr.1 provides a source-built static utility
binary, but its shell is unsuitable: it lacks the runner's `set -o pipefail`
semantics. The source check prevents a known-invalid remote build. Dash's
implemented pipefail and source-built BusyBox ash are being compared for
compatibility and maintainable build integration. Toybox is not a required
choice. No upstream runner behavior will be weakened to fit a replacement.

GNU timeout is currently an explicitly selected prebuilt package executable;
removing the parent OCI image alone does not make that binary source-built.
Qualify both architecture link closures, shell/timeout behavior and the
actual seven PIDFD programs before accepting a replacement. The immutable
runtime inventory is `pidfd-scratch-base-audit.json`.

Prefer reusable source executables with explicit per-image installed paths.
Do not replace one implicit default utility image with another. No new native
test runner or generic image framework is required for this experiment.

WorkMap continues the separately approved resource-file relocations; these
do not change runtime bases. New image openings wait only where their base
actually needs correction. Existing scratch-based changes retain approvals.
