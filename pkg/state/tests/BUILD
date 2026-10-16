load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "tests",
    srcs = [
        "array.go",
        "bench.go",
        "integer.go",
        "load.go",
        "map.go",
        "record.go",
        "record_mode_norace.go",
        "record_mode_race.go",
        "reference_array.go",
        "register.go",
        "struct.go",
        "tests.go",
    ],
    deps = [
        "//pkg/state",
        "//pkg/state/pretty",
    ],
)

go_test(
    name = "tests_test",
    size = "small",
    srcs = [
        "array_test.go",
        "bench_test.go",
        "binary_test.go",
        "bool_test.go",
        "float_test.go",
        "integer_test.go",
        "load_test.go",
        "map_test.go",
        "reference_array_test.go",
        "register_test.go",
        "snapshot_test.go",
        "string_test.go",
        "struct_test.go",
    ],
    embed = [":tests"],
    deps = [
        "//pkg/state",
        "//pkg/state/wire",
        "//pkg/tcpip",
    ],
)

# Composition-only race owner; same sources and dependencies as the suite above.
go_test(
    name = "tests_test_slice_diagnostic_race",
    size = "small",
    srcs = [
        "array_test.go",
        "bench_test.go",
        "binary_test.go",
        "bool_test.go",
        "float_test.go",
        "integer_test.go",
        "load_test.go",
        "map_test.go",
        "reference_array_test.go",
        "register_test.go",
        "snapshot_test.go",
        "string_test.go",
        "struct_test.go",
    ],
    architectures = [],
    embed = [":tests"],
    nogo = False,
    race = "on",
    tags = ["manual"],
    deps = [
        "//pkg/state",
        "//pkg/state/wire",
        "//pkg/tcpip",
    ],
)
