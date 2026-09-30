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
    # Match externalUDSSocketPath in test/e2e/uds_proxy_test.go.
    "-net-uds": ["--network-proxy-path=/tmp/gvisor-net-uds/proxy.sock"],
    "-overlay": ["--overlay2=all:self"],
    "-cgroupv2": ["--in-sandbox-cgroup=v2"],
}

# Preserve the public overlay, software-GSO and host-network integration lanes.
# The unnamed variant keeps each suite's existing default owned test.
INTEGRATION_VARIANTS = [
    struct(name = "", args = []),
    struct(
        name = "overlay",
        args = ["--overlay2=all:dir=/tmp"],
        test_args = ["--test-overlay=true"],
    ),
    struct(name = "swgso", args = ["--software-gso=true", "--gso=false"]),
    struct(
        name = "hostnet",
        args = ["--network=host", "--net-raw"],
        test_args = ["--test-checkpoint=false", "--test-hostnet=true", "--test-net-raw=true"],
    ),
]

# The public plugin lane uses its own runtime and only ConnectToSelf cases.
PLUGIN_NETWORK_VARIANT = struct(
    name = "plugin_network",
    args = ["--network=plugin"],
    runtime = Label("//:plugin_release"),
    tags = ["network_plugins"],
    test_args = ["-test.run=ConnectToSelf"],
)

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
        "integration": [variant for variant in INTEGRATION_VARIANTS if variant.name],
        "integration-docker": [
            struct(name = variant.name, args = variant.args + RUNTIME_VARIANTS["-docker"])
            for variant in INTEGRATION_VARIANTS
            if variant.name
        ],
        "plugin-network": [PLUGIN_NETWORK_VARIANT],
        "portforward": PORTFORWARD_VARIANTS,
        "posture": POSTURE_VARIANTS,
        "netfilter": NETFILTER_VARIANTS.values(),
    }.items()
}

# Image names are the existing Docker test inputs, grouped by their consuming
# suite. MODULE.bazel pins the matching tools/images.mk artifacts by digest.
COHORT_IMAGES = {
    "uds_proxy": ["basic/alpine"],
    "iptables": ["iptables"],
    "nftables": ["nftables"],
    "packetdrill": ["packetdrill"],
    "containerd": ["containerd/harness"],
    "startup": ["benchmarks/alpine"],
    "benchmark_absl": [
        "benchmarks/absl",
    ],
    "benchmark_ffmpeg": [
        "benchmarks/ffmpeg",
    ],
    "benchmark_fio": [
        "benchmarks/fio",
    ],
    "benchmark_grpc": [
        "benchmarks/build-grpc",
    ],
    "benchmark_hackbench": [
        "benchmarks/hackbench",
    ],
    "benchmark_httpd": [
        "benchmarks/hey",
        "benchmarks/httpd",
        "benchmarks/util",
    ],
    "benchmark_iperf": [
        "benchmarks/iperf",
    ],
    "benchmark_nginx": [
        "benchmarks/hey",
        "benchmarks/nginx",
        "benchmarks/util",
    ],
    "benchmark_node": [
        "benchmarks/hey",
        "benchmarks/node",
        "benchmarks/redis",
        "benchmarks/util",
    ],
    "benchmark_redis": [
        "benchmarks/redis",
    ],
    "benchmark_ruby": [
        "benchmarks/hey",
        "benchmarks/redis",
        "benchmarks/ruby",
        "benchmarks/util",
    ],
    "benchmark_rubydev": [
        "benchmarks/rubydev",
    ],
    "benchmark_servers": [
        "benchmarks/alpine",
        "benchmarks/nginx",
        "benchmarks/node",
        "benchmarks/redis",
        "benchmarks/util",
    ],
    "benchmark_sysbench": [
        "benchmarks/sysbench",
    ],
    "benchmark_syscallbench": [
        "benchmarks/syscallbench",
    ],
    "benchmark_tensorflow": [
        "benchmarks/tensorflow",
    ],
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

# These benchmark contexts have no canonical ARM64 Dockerfile or image.
AMD64_IMAGES = AMD64_RUNTIME_IMAGES + [
    "benchmarks/absl",
    "benchmarks/syscallbench",
    "benchmarks/tensorflow",
]

# The public integration variants select these same two suites.
INTEGRATION_TESTS = [
    "//test/image:image_test",
    "//test/e2e:integration_test",
]

# The maintained Make Docker lane, shared by installed and owned test suites.
DOCKER_TESTS = INTEGRATION_TESTS + [
    "//test/e2e:integration_runtime_test",
    "//test/e2e:runtime_in_docker_test",
    "//test/e2e:uds_proxy_test",
]
