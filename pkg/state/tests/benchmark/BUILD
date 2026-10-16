load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Common measurement-only fixture; both arms use this exact source set.
go_library(
    name = "benchmark",
    testonly = True,
    srcs = ["bench.go"],
    deps = ["//pkg/state"],
)

go_test(
    name = "benchmark_test",
    size = "small",
    srcs = ["bench_test.go"],
    embed = [":benchmark"],
    deps = [
        "//pkg/state",
        "//pkg/tcpip",
    ],
)

# Composition-only owners keep each existing leaf in a fresh process.
[
    go_test(
        name = "slice_diagnostic_" + name,
        size = "small",
        srcs = ["bench_test.go"],
        architectures = [],
        args = [
            "-test.run=^$$",
            "-test.bench=" + benchmark,
            "-test.benchtime=1x",
            "-test.benchmem",
            "-test.count=1",
            "-test.cpu=1",
        ],
        embed = [":benchmark"],
        env = {
            "GODEBUG": "inittrace=1",
            "GOMAXPROCS": "1",
        },
        nogo = False,
        tags = ["manual"],
        deps = [
            "//pkg/state",
            "//pkg/tcpip",
        ],
    )
    for name, benchmark in [
        ("empty", "^BenchmarkFirstRestore$$/^empty$$"),
        ("ordinary", "^BenchmarkFirstRestore$$/^ordinary$$"),
        ("adoption", "^BenchmarkDecoding$$/^adopt_captured$$/^state$$"),
        ("control_save", "^BenchmarkControlMessageEncoding$$"),
    ]
]
