load("@rules_python//python:py_test.bzl", "py_test")

# Diagnostic-only inputs come from two sources under two linker layouts.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    main = "run.py",
    args = ["$(rootpath @org_golang_x_perf//cmd/benchstat)"],
    data = [
        "inputs.json",
        "arms/before_default/_main/pkg/tcpip/transport/udp/udp_test",
        "arms/after_default/_main/pkg/tcpip/transport/udp/udp_test",
        "arms/before_align64/_main/pkg/tcpip/transport/udp/udp_test",
        "arms/after_align64/_main/pkg/tcpip/transport/udp/udp_test",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    tags = ["manual"],
)
