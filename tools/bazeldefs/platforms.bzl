"""Platforms and remote test requirements."""

# BuildBuddy's AMD64 runtime image supplies Docker tools for owned daemons.
# https://www.buildbuddy.io/docs/config-all-options/
_RBE_DOCKER_IMAGE = "docker://gcr.io/flame-public/buildbuddy-ci-runner@sha256:8cf614fc4695789bea8321446402e7d6f84f6be09b8d39ec93caa508fa3e3cfc"

# AMD64/ARM64 networking runtime with CA trust, iproute2 and OpenBSD netcat.
# The compilation image has no CA bundle. This image's provenance identifies
# the source revision below; no compiler tools are added.
# https://github.com/istio/istio/blob/1d6649895/docker/Dockerfile.base
_RBE_NETWORK_TOOLS_IMAGE = "docker://docker.io/istio/base@sha256:cab6852ff5ae39349136f41af6ee892a228c8fb9634ec25e7550bd9b517a7a93"

def network_test_exec_properties():
    """Returns remote test properties for external HTTPS access."""
    return select({
        Label("//tools/bazeldefs:rbe"): {
            "test.container-image": _RBE_NETWORK_TOOLS_IMAGE,
            "test.dockerUser": "nobody",
            "test.network": "external",
            "test.nonroot-workspace": "true",
            "test.workload-isolation-type": "oci",
        },
        "//conditions:default": {},
    })

def docker_test_exec_properties(free_disk, memory = None):
    """Returns a remote VM with Docker tools for a test-owned daemon.

    Args:
      free_disk: Root filesystem space for expanded images and container copies.
      memory: Test VM memory budget; defaults to 4GB.
    """
    return select({
        Label("//tools/bazeldefs:rbe"): {
            "test.EstimatedCPU": "4",
            "test.EstimatedMemory": memory if memory != None else "4GB",
            "test.EstimatedFreeDiskBytes": free_disk,
            "test.container-image": _RBE_DOCKER_IMAGE,
            "test.dockerUser": "root",
            "test.network": "external",
            "test.network-enable-ipv6": "true",
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
        Label("//tools/bazeldefs:rbe"): _namespace_exec_properties(user),
        "//conditions:default": {},
    })

def _namespace_exec_properties(user):
    return {
        "test.dockerUser": user,
        # Firecracker otherwise boots with ipv6.disable=1.
        "test.network-enable-ipv6": "true",
        "test.workload-isolation-type": "firecracker",
    }

def syscall_test_exec_properties(platform, network_tools = False):
    """Returns defaults for remote syscall test execution.

    Args:
      platform: Native or runsc platform used by the test runner.
      network_tools: Supply iproute2 and OpenBSD netcat in the test image.

    Returns:
      Test-runner properties; compilation keeps the execution platform's defaults.
    """
    properties = {}

    # KVM and slimvm need separate worker contracts.
    if platform in ("native", "ptrace", "systrap"):
        properties.update(_namespace_exec_properties("root"))
    if network_tools:
        properties["test.container-image"] = _RBE_NETWORK_TOOLS_IMAGE
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
