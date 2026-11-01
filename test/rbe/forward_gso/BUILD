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
    args = ["gvisor-source-segmented"],
    data = [":router", "//:release"],
    local = True,
)
