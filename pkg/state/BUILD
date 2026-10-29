load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "state",
    srcs = [
        "addrmap.go",
        "buf_unsafe.go",
        "decode_unsafe.go",
        "encode_unsafe.go",
        "print_unsafe.go",
        "state.go",
        "state_norace.go",
        "state_race.go",
        "stats.go",
        "typed.go",
        "typeinfo_unsafe.go",
        "types.go",
    ],
    importpath = "gvisor.dev/gvisor/pkg/state",
    marshal = False,
    stateify = False,
    visibility = [
        "//visibility:public",
    ],
    deps = [
        "//pkg/gohacks",
        "//pkg/state/wire",
    ],
)

go_test(
    name = "state_test",
    size = "small",
    srcs = [
        "addrmap_test.go",
        "callback_order_test.go",
        "typeinfo_test.go",
        "types_test.go",
    ],
    embed = [":state"],
)
