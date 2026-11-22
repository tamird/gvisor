load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//tools:defs.bzl", "build_test", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

sh_binary(
    name = "native_worker",
    srcs = ["native_worker.sh"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
)

go_library(
    name = "harness",
    testonly = 1,
    srcs = [
        "docker_worker_linux.go",
        "docker_worker_unsupported.go",
        "harness.go",
        "machine.go",
        "util.go",
    ],
    importpath = "gvisor.dev/gvisor/test/benchmarks/harness",
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/cleanup",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/image:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
    ],
)

# Select the entrypoint by producer metadata while retaining the complete
# release fileset in the runtime test's data.
filegroup(
    name = "runsc_entrypoint",
    srcs = ["//:release"],
    output_group = "runsc",
    visibility = ["//test/benchmarks/network:__pkg__"],
)

_PAIRED_BENCHMARKS = [
    (
        "iperf",
        "//test/benchmarks/network:iperf_test",
        8,
        [
            "//images/benchmarks/iperf:iperf_archive",
        ],
    ),
]

[
    go_test(
        name = "paired_" + category + "_worker_test",
        size = "large",
        timeout = "long",
        srcs = [
            "paired_products_test.go",
            "paired_worker_test.go",
        ],
        args = [
            "--paired-base-runsc=$(rootpath :runsc_entrypoint)",
            "--paired-candidate-runsc=$(rootpath :runsc_entrypoint)",
            "--paired-base-source=eb263e1f414d0bceaf10decea6c73e2c77332112",
            "--paired-candidate-source=eb263e1f414d0bceaf10decea6c73e2c77332112",
            "--paired-benchmark=$(rootpath %s)" % binary,
            "--paired-leaves=%d" % leaves,
            "--paired-image-archives='%s'" % " ".join(["$(rootpaths %s)" % archive for archive in archives]),
            "--client-cpuset=0-1",
            "--server-cpuset=2-3",
            "-test.run=^$$",
        ],
        data = [
            ":runsc_entrypoint",
            "//:release",
            binary,
        ] + archives,
        embed = [":harness"],
        exec_properties = {
            "test.EstimatedCPU": "4",
            "test.EstimatedMemory": "2G" if category == "ruby" else "500M",
            "test.init-dockerd": "false",
            "test.workload-isolation-type": "firecracker",
        },
        pure = True,
        tags = ["manual"],
        target_compatible_with = [
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ],
        deps = [
            "//pkg/test/testutil",
            "@com_github_docker_docker//api/types/container:go_default_library",
            "@com_github_docker_docker//api/types/image:go_default_library",
            "@com_github_docker_docker//client:go_default_library",
            "@org_golang_x_sys//unix:go_default_library",
        ],
    )
    for category, binary, leaves, archives in _PAIRED_BENCHMARKS
]
