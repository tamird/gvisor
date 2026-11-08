load("//runsc/version:defs.bzl", "VERSION_X_DEFS")
load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "checkpointgofer_binary",
    srcs = ["main.go"],
    pure = True,
    visibility = [
        "//visibility:public",
    ],
    x_defs = VERSION_X_DEFS,
    deps = [
        "//pkg/sentry/state/stateipc",
        "//pkg/unet",
        "//pkg/urpc",
        "//runsc/checkpointgofer/gcs",
        "//runsc/cli",
        "//runsc/cmd/util",
        "//runsc/flag",
        "//runsc/gvisorbinaries",
        "@com_github_google_subcommands//:go_default_library",
        "@org_golang_x_oauth2//:go_default_library",
    ],
)
