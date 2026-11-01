load("//tools:defs.bzl", "go_binary")
load("@rules_cc//cc:cc_binary.bzl", "cc_binary")
load("@rules_python//python:defs.bzl", "py_test")

cc_binary(
    name = "router",
    srcs = ["router.c"],
    linkopts = ["-static"],
)

py_test(
    name = "linux_forwarding_test",
    size = "small",
    srcs = ["forwarding_test.py"],
    args = ["linux"],
    local = True,
    main = "forwarding_test.py",
)

py_test(
    name = "forwarding_test",
    size = "large",
    srcs = ["forwarding_test.py"],
    args = ["aggregate4","segmented4","aggregate6","segmented6"],
    data = [":router", "//:release"],
    local = True,
)

go_binary(
    name = "bridge",
    testonly = True,
    srcs = ["bridge.go", "inner_capture.go"],
    pure = "on",
    deps = ["@com_github_vishvananda_netlink//:go_default_library"],
)

py_test(
    name = "nested_test",
    size = "large",
    srcs = ["nested_test.py", "forwarding_test.py"],
    data = [":bridge", "//:release"],
    imports = ["."],
    local = True,
)
