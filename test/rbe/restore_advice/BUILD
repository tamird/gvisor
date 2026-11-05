load("//tools:defs.bzl", "go_binary")

go_binary(
    name = "guest",
    testonly = True,
    srcs = ["guest.go"],
    visibility = ["//visibility:public"],
    deps = ["@org_golang_x_sys//unix:go_default_library"],
)

go_binary(
    name = "inspect",
    testonly = True,
    srcs = ["inspect.go"],
    deps = [
        "//pkg/state",
        "//pkg/state/statefile",
        "//pkg/state/wire",
    ],
)
