"""Platforms and remote syscall test requirements."""

def syscall_test_exec_properties(platform):
    """Returns defaults for remote syscall test execution.

    Args:
      platform: Native or runsc platform used by the test runner.

    Returns:
      Test-runner properties; compilation keeps the execution platform's defaults.
    """
    properties = {}

    # KVM and slimvm need separate worker contracts.
    if platform in ("native", "ptrace", "systrap"):
        properties = {
            "test.dockerUser": "root",
            # Firecracker otherwise boots with ipv6.disable=1.
            "test.network-enable-ipv6": "true",
            "test.workload-isolation-type": "firecracker",
        }
    return select({
        Label("//tools/bazeldefs:rbe"): properties,
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
