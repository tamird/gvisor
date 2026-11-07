load("//tools:defs.bzl", "cc_test", "namespace_test_exec_properties")

cc_test(
    name = "accounting",
    size = "small",
    srcs = ["accounting.cc"],
    architectures = ["amd64", "arm64"],
    exec_properties = namespace_test_exec_properties,
    linkopts = ["-pthread"],
    tags = ["manual"],
)
