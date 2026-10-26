load("//tools:defs.bzl", "go_binary")
load(":defs.bzl", "syzkaller_build", "syzkaller_smoke_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files([
    "smoke_report.patch",
    "upstream.BUILD",
])

go_binary(
    name = "stage",
    srcs = ["main.go"],
    pure = True,
)

syzkaller_build(
    name = "syzkaller",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = {
        "EstimatedCPU": "4",
        "EstimatedMemory": "8GB",
    },
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

syzkaller_smoke_test(
    name = "smoke_test",
    size = "large",
    timeout = "eternal",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    # Upstream's smoke script uses sudo, tar, mount and coreutils.
    # Keep the previously exercised utility image; compilers come from Bazel.
    exec_properties = select({
        "//tools/bazeldefs:rbe": {
            "test.EstimatedCPU": "4",
            "test.EstimatedMemory": "8GB",
            "test.EstimatedFreeDiskBytes": "40GB",
            "test.container-image": "docker://gcr.io/syzkaller/syzbot@sha256:e15c2ce66ed56f62a80d6f3642b888f89c1e0bba5b9868e8ce420656e9926e42",
            "test.dockerUser": "root",
            "test.network-enable-ipv6": "true",
            "test.workload-isolation-type": "firecracker",
        },
        "//conditions:default": {},
    }),
    runtime = "//debian:gvisor-release-tar-bz2",
    syzkaller = ":syzkaller",
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)
