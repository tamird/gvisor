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

## First implementation

Hostinet owns the PIDFD prototype on `rules-img-pidfd-scratch`, preserving
approved4be5. BCR Toybox0.8.12.bcr.1 provides a source-built static target;
its utility applets are a candidate, but its shell lacks the pipefail
behavior required by the retained upstream runner. A mature source-built
shell is still being selected. Keep upstream kselftest scripts and GNU
timeout. Qualify both architecture link closures, shell/timeout behavior
and the actual seven programs before claiming that the replacement works. The immutable detailed audit is
`pidfd-scratch-base-audit.json`.

Prefer a reusable executable layer with explicit per-image applet symlinks.
Do not replace one implicit default utility image with another. No new native
test runner or generic image framework is required for this experiment.

WorkMap continues the separately approved resource-file relocations; these
do not change runtime bases. New image openings wait only where their base
actually needs correction. Existing scratch-based changes retain approvals.
