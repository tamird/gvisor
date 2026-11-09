load("//tools:defs.bzl", "go_test")

go_test(
    name = "exitstatus_test",
    srcs = ["runner_exit_test.go"],
    data = ["//test/packetimpact/runner:main"],
    exec_properties = {
        "test.dockerUser": "root",
        "test.workload-isolation-type": "firecracker",
    },
    tags = ["manual"],
    deps = ["//pkg/test/testutil"],
)
