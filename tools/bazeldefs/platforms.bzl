"""Platforms and remote test requirements."""

# BuildBuddy's Ubuntu 22.04 image supplies iptables 1.8.7 and Docker 24.
# Docker 28 breaks checkpoint/restore: https://github.com/moby/moby/issues/50750.
RBE_DOCKER_TOOLS_IMAGE = "docker://gcr.io/flame-public/rbe-ubuntu22-04@sha256:0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3"

# AMD64/ARM64 networking runtime with CA trust, iptables-nft, iproute2 and
# OpenBSD netcat.
# The compilation image has no CA bundle. This image's provenance identifies
# the source revision below; no compiler tools are added.
# https://github.com/istio/istio/blob/1d6649895/docker/Dockerfile.base
RBE_NETWORK_TOOLS_IMAGE = "docker://docker.io/istio/base@sha256:cab6852ff5ae39349136f41af6ee892a228c8fb9634ec25e7550bd9b517a7a93"

def network_test_exec_properties():
    """Returns remote test properties for external HTTPS access."""
    return select({
        # The cgroup-v1 wrapper needs root and mount tools before the test starts.
        # The Docker image also supplies CA trust for the HTTPS requests.
        Label("//tools/bazeldefs:rbe_cgroup_v1"): docker_exec_properties(free_disk = "20GB"),
        Label("//tools/bazeldefs:rbe"): {
            "test.container-image": RBE_NETWORK_TOOLS_IMAGE,
            "test.dockerUser": "nobody",
            "test.network": "external",
            "test.nonroot-workspace": "true",
            "test.workload-isolation-type": "oci",
        },
        "//conditions:default": {},
    })

def docker_test_exec_properties(free_disk, memory = None, exec_group = "test"):
    """Selects Docker VM properties when remote execution is enabled."""
    return select({
        Label("//tools/bazeldefs:rbe"): docker_exec_properties(free_disk, memory, exec_group),
        "//conditions:default": {},
    })

def docker_exec_properties(free_disk, memory = None, exec_group = "test"):
    """Returns a remote VM with Docker tools for an owned daemon.

    Args:
      free_disk: Root filesystem space for expanded images and container copies.
      memory: VM memory budget; defaults to 4GB.
      exec_group: Group owning the daemon; empty uses the rule's default group.
    """
    prefix = exec_group + "." if exec_group else ""
    return {
        prefix + "EstimatedCPU": "4",
        prefix + "EstimatedMemory": memory if memory != None else "4GB",
        prefix + "EstimatedFreeDiskBytes": free_disk,
        prefix + "container-image": RBE_DOCKER_TOOLS_IMAGE,
        prefix + "dockerUser": "root",
        prefix + "network": "external",
        prefix + "network-enable-ipv6": "true",
        prefix + "workload-isolation-type": "firecracker",
        # Owned daemons change guest-wide kernel state. Discard their VM.
        prefix + "recycle-runner": "false",
    }

def namespace_test_exec_properties(user = "root", image = None, memory = None):
    """Defaults for remote tests that create nested Linux namespaces.

    Args:
      user: Identity to use inside the remote test VM.
      image: Optional image supplying the test's runtime tools.
      memory: Optional remote test memory budget.

    Returns:
      Test-runner properties; compilation keeps the execution platform's defaults.
    """
    properties = _namespace_exec_properties(user)
    if image != None:
        properties["test.container-image"] = image
    if memory != None:
        properties["test.EstimatedMemory"] = memory
    return select({
        Label("//tools/bazeldefs:rbe"): properties,
        "//conditions:default": {},
    })

def _namespace_exec_properties(user):
    return {
        "test.dockerUser": user,
        # Firecracker otherwise boots with ipv6.disable=1.
        "test.network-enable-ipv6": "true",
        "test.workload-isolation-type": "firecracker",
    }

def syscall_test_exec_properties(platform, network_tools = False, memory = None):
    """Returns defaults for remote syscall test execution.

    Args:
      platform: Native or runsc platform used by the test runner.
      network_tools: Supply iproute2 and OpenBSD netcat in the test image.
      memory: Optional remote test memory budget.

    Returns:
      Test-runner properties; compilation keeps the execution platform's defaults.
    """
    properties = {}

    # KVM and slimvm need separate worker contracts.
    if platform in ("native", "ptrace", "systrap"):
        properties.update(_namespace_exec_properties("root"))
    if network_tools:
        properties["test.container-image"] = RBE_NETWORK_TOOLS_IMAGE
    if memory != None:
        properties["test.EstimatedMemory"] = memory
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
