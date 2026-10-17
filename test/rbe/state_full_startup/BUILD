load("@rules_python//python:py_test.bzl", "py_test")

exports_files(
    ["sandbox.py"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "releases",
    srcs = [
        "arms/after/release/containerd-shim-runsc-v1",
        "arms/after/release/gvisor-bin/checkpointgofer",
        "arms/after/release/gvisor-bin/gvisor-proc-umounter",
        "arms/after/release/gvisor-bin/gvisor-sentry-prewarmer",
        "arms/after/release/gvisor-bin/gvisor_sentry",
        "arms/after/release/gvisor-bin/runsc-fd-parking",
        "arms/after/release/gvisor-bin/runsc-metric-server",
        "arms/after/release/runsc",
        "arms/before/release/containerd-shim-runsc-v1",
        "arms/before/release/gvisor-bin/checkpointgofer",
        "arms/before/release/gvisor-bin/gvisor-proc-umounter",
        "arms/before/release/gvisor-bin/gvisor-sentry-prewarmer",
        "arms/before/release/gvisor-bin/gvisor_sentry",
        "arms/before/release/gvisor-bin/runsc-fd-parking",
        "arms/before/release/gvisor-bin/runsc-metric-server",
        "arms/before/release/runsc",
        "release-inputs.json",
    ],
    visibility = ["//visibility:public"],
)

py_test(
    name = "cli_test",
    size = "large",
    srcs = [
        "cli.py",
        "cli_batch.py",
    ],
    args = ["$(rootpath @org_golang_x_perf//cmd/benchstat)"],
    data = [
        ":releases",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    exec_properties = {
        "test.EstimatedCPU": "4",
        "test.EstimatedMemory": "16GB",
    },
    main = "cli.py",
    tags = ["manual"],
)
