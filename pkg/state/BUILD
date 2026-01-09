load("//tools:defs.bzl", "go_library")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_template_instance(
    name = "addr_set",
    out = "addr_set.go",
    consts = {
        "minDegree": "10",
    },
    imports = {
        "reflect": "reflect",
    },
    package = "state",
    prefix = "addr",
    template = "//pkg/segment:generic_set",
    types = {
        "Key": "uintptr",
        "Range": "addrRange",
        "Value": "*objectEncodeState",
        "Functions": "addrSetFunctions",
    },
)

go_library(
    name = "state",
    srcs = [
        "addr_set.go",
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
        "//pkg/state/wire",
    ],
)
