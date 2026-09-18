load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "crosspkg",
    srcs = [
        "crosspkg.go",
        "pointer_globals.go",
    ],
    # See next level up.
    marshal = False,
    stateify = False,
    visibility = ["//tools/checklocks/test:__pkg__"],
)
