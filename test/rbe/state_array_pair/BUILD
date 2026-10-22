load("@rules_python//python:py_test.bzl", "py_test")

# Diagnostic-only input files are staged from the two source-bound builds.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    main = "run.py",
    args = ["--profiles"],
    data = [
        "inputs.json",
        "arms/before/_main/pkg/tcpip/transport/udp/udp_test",
        "arms/after/_main/pkg/tcpip/transport/udp/udp_test",
    ],
    tags = ["manual"],
)
