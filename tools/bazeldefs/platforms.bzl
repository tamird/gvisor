"""List of platforms."""

def namespace_test_exec_properties(user = "root"):
    """Defaults for remote tests that create nested Linux namespaces.

    Args:
      user: Identity to use inside the remote test VM.

    Returns:
      Per-test properties; the execution platform supplies the image and CPU/OS.
    """
    return select({
        Label("//tools/bazeldefs:rbe"): {
            "dockerUser": user,
            "workload-isolation-type": "firecracker",
        },
        "//conditions:default": {},
    })

# Platform to associated tags.
platforms = {
    "ptrace": [],
    "kvm": [],
    "slimvm": ["manual", "requires-slimvm"],
    "systrap": [],
}

# Capabilities that platforms may or may not support.
# Used by platform_util.cc to determine which syscall tests are appropriate.
_CAPABILITY_32BIT = "32BIT"
_CAPABILITY_ALIGNMENT_CHECK = "ALIGNMENT_CHECK"
_CAPABILITY_MULTIPROCESS = "MULTIPROCESS"
_CAPABILITY_INT3 = "INT3"
_CAPABILITY_VSYSCALL = "VSYSCALL"

# platform_capabilities maps platform names to a dictionary of capabilities mapped to
# True (supported) or False (unsupported).
platform_capabilities = {
    "ptrace": {
        _CAPABILITY_32BIT: False,
        _CAPABILITY_ALIGNMENT_CHECK: True,
        _CAPABILITY_MULTIPROCESS: True,
        _CAPABILITY_INT3: True,
        _CAPABILITY_VSYSCALL: True,
    },
    "systrap": {
        _CAPABILITY_32BIT: False,
        _CAPABILITY_ALIGNMENT_CHECK: True,
        _CAPABILITY_MULTIPROCESS: True,
        _CAPABILITY_INT3: True,
        _CAPABILITY_VSYSCALL: True,
    },
    "kvm": {
        _CAPABILITY_32BIT: False,
        _CAPABILITY_ALIGNMENT_CHECK: True,
        _CAPABILITY_MULTIPROCESS: True,
        _CAPABILITY_INT3: False,
        _CAPABILITY_VSYSCALL: True,
    },
    "slimvm": {
        _CAPABILITY_32BIT: False,
        _CAPABILITY_ALIGNMENT_CHECK: True,
        _CAPABILITY_MULTIPROCESS: True,
        _CAPABILITY_INT3: False,
        _CAPABILITY_VSYSCALL: True,
    },
}

default_platform = "systrap"
save_restore_platforms = ["systrap"]
