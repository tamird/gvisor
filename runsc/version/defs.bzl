"""Link inputs for programs that report the gVisor build version."""

# Some callers supply only STABLE_VERSION from their own status command.
VERSION_X_DEFS = {
    "gvisor.dev/gvisor/runsc/version.buildLabel": "{BUILD_EMBED_LABEL}",
    "gvisor.dev/gvisor/runsc/version.version": "{STABLE_VERSION}",
}
