"""Shared configuration for the Docker integration suites."""

load("//tools/bazeldefs:platforms.bzl", "platforms")

# Runtime suffixes used by MakeContainerWithRuntime. Both the private test
# daemon and the installed-runtime adapter consume this same table.
RUNTIME_VARIANTS = {
    "": [],
    "-docker": ["--net-raw", "--allow-packet-socket-write"],
    "-fdlimit": ["--fdlimit=2000"],
    "-dcache": ["--fdlimit=2000", "--dcache=100"],
    "-host-uds": ["--host-uds=all"],
    "-overlay": ["--overlay2=all:self"],
    "-cgroupv2": ["--in-sandbox-cgroup=v2"],
}

# Port forwarding is qualified with both runtime network implementations. The
# installed-runtime adapter and owned Bazel actions use these same modes.
PORTFORWARD_VARIANTS = [
    struct(name = "sandbox", args = ["--network=sandbox"]),
    struct(name = "host", args = ["--network=host"]),
]

# Runtime modes used by the installed and owned netfilter suites.
NETFILTER_VARIANTS = {
    "iptables": struct(name = "iptables", args = ["--net-raw"]),
    "reproduce": struct(name = "reproduce", args = ["--net-raw", "--reproduce-nftables"]),
    "nftables": struct(name = "nftables", args = ["--net-raw", "--TESTONLY-nftables"]),
}

# Nftables conformance runs against both the sentry and native Linux.
NFTABLES_VARIANTS = [
    NETFILTER_VARIANTS["nftables"],
    struct(name = "native", args = [], test_args = ["--runtime=runc"]),
]

# Preserve the full matrix from Make's sandbox-posture-tests, including KVM.
POSTURE_VARIANTS = [
    struct(name = "default", args = []),
    struct(name = "hostnet", args = ["--network=host"]),
    struct(name = "hostnet_raw", args = ["--network=host", "--net-raw"]),
    struct(name = "nodirectfs", args = ["--directfs=false"]),
    struct(name = "nodirectfs_hostnet", args = ["--directfs=false", "--network=host"]),
    struct(name = "kvm", args = ["--platform=kvm"], tags = ["requires-kvm"]),
]

# Match benchmark-platforms' public runtime selection and profiling flags.
# Native runc is selected through the test flag, never registered as runsc.
STARTUP_VARIANTS = [
    struct(
        name = platform,
        args = ["--platform=" + platform, "--profile"],
        tags = platforms[platform] + (["requires-kvm"] if platform == "kvm" else []),
    )
    for platform in sorted(platforms)
    if "internal" not in platforms[platform] and platform != "slimvm"
] + [struct(name = "runc", args = [], test_args = ["--runtime=runc"])]

# The Go runtime adapter consumes names and arguments as JSON, without the
# build-only tags on individual variants.
RUNTIME_SUITES = {
    suite: [struct(name = variant.name, args = variant.args) for variant in variants]
    for suite, variants in {
        "docker": [struct(name = name, args = args) for name, args in RUNTIME_VARIANTS.items()],
        "portforward": PORTFORWARD_VARIANTS,
        "posture": POSTURE_VARIANTS,
        "netfilter": NETFILTER_VARIANTS.values(),
    }.items()
}

# Image names are the existing Docker test inputs, grouped by their consuming
# suite. MODULE.bazel pins the matching tools/images.mk artifacts by digest.
COHORT_IMAGES = {
    "iptables": ["iptables"],
    "nftables": ["nftables"],
    "packetdrill": ["packetdrill"],
    "containerd": ["containerd/harness"],
    "startup": ["benchmarks/alpine"],
    "posture": ["basic/alpine"],
    "portforward": [
        "basic/nginx",
        "basic/redis",
    ],
    "root": [
        "basic/alpine",
        "basic/ubuntu",
    ],
    "integration": [
        "basic/alpine",
        "basic/filecap",
        "basic/integrationtest",
        "basic/libacl",
        "basic/nginx",
        "basic/python",
        "basic/sudo",
        "basic/tmpfile",
        "basic/ubuntu",
    ],
    "runtime": [
        "basic/alpine",
        "basic/integrationtest",
        "basic/pidfd-tests",
        "basic/ubuntu",
        "systemd-integ",
        "systemd-services",
        "ubi10-init",
    ],
    "nested_runtime": [
        "basic/integrationtest",
    ],
    "image": [
        "basic/alpine",
        "basic/docker",
        "basic/httpd",
        "basic/mysql",
        "basic/nginx",
        "basic/rust",
        "basic/tcpdump",
        "basic/tomcat",
        "image-test/ruby",
    ],
}

# These archives are imported directly by CRI inside the containerd harness.
CONTAINERD_IMAGES = [
    "basic/alpine",
    "basic/python",
    "basic/busybox",
    "basic/symlink-resolv",
    "basic/httpd",
    "basic/ubuntu",
]

# This existing systemd case only runs on AMD64.
AMD64_RUNTIME_IMAGES = ["arch-systemd"]

# The maintained Make Docker lane, shared by installed and owned test suites.
DOCKER_TESTS = [
    "//test/image:image_test",
    "//test/e2e:integration_test",
    "//test/e2e:integration_runtime_test",
    "//test/e2e:runtime_in_docker_test",
]
