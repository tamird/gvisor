load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "state",
    srcs = [
        "debug.go",
        "decode.go",
        "decode_unsafe.go",
        "encode.go",
        "encode_unsafe.go",
        "state.go",
        "state_norace.go",
        "state_race.go",
        "stats.go",
        "types.go",
    ],
    marshal = False,
    stateify = False,
    visibility = [
        "//visibility:public",
    ],
    deps = [
        "//pkg/ilist",
        "//pkg/segment",
        "//pkg/segment/set10",
        "//pkg/state/wire",
    ],
)
