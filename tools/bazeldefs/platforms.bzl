"""Platforms and remote test requirements."""

# The compilation image has no CA bundle. BuildBuddy's runtime image includes
# system trust for tests that contact external services over HTTPS.
# https://www.buildbuddy.io/docs/config-all-options/
_RBE_TEST_IMAGE = "docker://gcr.io/flame-public/buildbuddy-ci-runner@sha256:8cf614fc4695789bea8321446402e7d6f84f6be09b8d39ec93caa508fa3e3cfc"

def network_test_exec_properties():
    """Returns remote test properties for external HTTPS access."""
    return select({
        Label("//tools/bazeldefs:rbe"): {
            "test.container-image": _RBE_TEST_IMAGE,
            "test.dockerUser": "nobody",
            "test.network": "external",
            "test.nonroot-workspace": "true",
            "test.workload-isolation-type": "oci",
        },
        "//conditions:default": {},
    })

def docker_test_exec_properties():
    """Returns a remote VM with Docker tools for a test-owned daemon."""
    return select({
        Label("//tools/bazeldefs:rbe"): {
            "test.container-image": _RBE_TEST_IMAGE,
            "test.dockerUser": "root",
            "test.network": "external",
            "test.workload-isolation-type": "firecracker",
        },
        "//conditions:default": {},
    })

def namespace_test_exec_properties(user = "root"):
    """Defaults for remote tests that create nested Linux namespaces.

    Args:
      user: Identity to use inside the remote test VM.

    Returns:
      Test-runner properties; compilation keeps the execution platform's defaults.
    """
    return select({
        Label("//tools/bazeldefs:rbe"): {
            "test.dockerUser": user,
            # Firecracker otherwise boots with ipv6.disable=1.
            "test.network-enable-ipv6": "true",
            "test.workload-isolation-type": "firecracker",
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
