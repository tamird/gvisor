load("//external/tools/plugin-stack:defs.bzl", "plugin_build")
load("//tools:defs.bzl", "go_binary", "go_cov", "sh_test")
load("//tools/bazeldefs:platforms.bzl", "RBE_DOCKER_TOOLS_IMAGE")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

filegroup(
    name = "go_export_sources",
    srcs = glob(["*.go"]),
    visibility = ["//:__pkg__"],
)

go_binary(
    name = "runsc",
    srcs = ["main.go"],
    noasan = True,
    pure = True,
    tags = ["staging"],
    # Do NOT add new visibility entries here.
    # Users should depend on the gVisor release fileset,
    # not on this target directly.
    visibility = ["//:__subpackages__"],
    x_defs = {"gvisor.dev/gvisor/runsc/version.version": "{STABLE_VERSION}"},
    deps = [
        "//runsc/cli/maincli",
        "//runsc/version",
    ],
)

plugin_build(
    name = "runsc-plugin-stack-build",
    srcs = [":runsc-plugin-stack"],
    tags = [
        "manual",
        "network_plugins",
    ],
    target_compatible_with = ["@platforms//cpu:x86_64"],
    visibility = ["//visibility:public"],
)

go_binary(
    name = "runsc-plugin-stack",
    srcs = ["main_plugin_stack.go"],
    gotags = ["network_plugins"],
    nogo = False,
    pure = False,
    static = True,
    tags = ["network_plugins"],
    visibility = [
        "//visibility:public",
    ],
    x_defs = {"gvisor.dev/gvisor/runsc/version.version": "{STABLE_VERSION}"},
    deps = [
        "//pkg/sentry/socket/plugin/stack",
        "//runsc/cli/maincli",
        "//runsc/version",
    ],
)

# The runsc-race target is a race-compatible BUILD target. This must be built
# via: bazel build --features=race :runsc-race
#
# This is necessary because the race feature must apply to all dependencies
# due a bug in gazelle file selection.  The pure attribute must be off because
# the race detector requires linking with non-Go components, although we still
# require a static binary.
#
# Note that in the future this might be convertible to a compatible target by
# using the pure and static attributes within a select function, but select is
# not currently compatible with string attributes [1].
#
# [1] https://github.com/bazelbuild/bazel/issues/1698
go_binary(
    name = "runsc-race",
    srcs = ["main.go"],
    gotags = ["lockdep"],
    race = "on",
    static = True,
    visibility = [
        "//visibility:public",
    ],
    x_defs = {"gvisor.dev/gvisor/runsc/version.version": "{STABLE_VERSION}"},
    deps = [
        "//runsc/cli/maincli",
        "//runsc/version",
    ],
)

sh_test(
    name = "version_test",
    size = "small",
    srcs = ["version_test.sh"],
    args = ["$(location :runsc)"],
    data = [":runsc"],
)

genrule(
    name = "runsc_coverage",
    srcs = [
        "main.go",
        "//:go.mod",
        "//:go.sum",
        "//:gopath",
        "//tools:build_cover.sh",
        "//tools:go-allow-internal.patch",
    ],
    outs = ["runsc_cov"],
    cmd =
        "\"$(location //tools:build_cover.sh)\" " +
        "\"$(location //runsc:runsc_cov)\" " +
        "\"$(location //:gopath)\" " +
        "\"$(location //:go.mod)\" " +
        "\"$(location //:go.sum)\" " +
        "\"$(location main.go)\" " +
        "\"$(location //tools:go-allow-internal.patch)\" " +
        "\"false\" ",
    # Keep the legacy helper's host utilities local to this action. All
    # transitive C/BPF prerequisites retain their declared toolchains.
    exec_properties = {
        "EstimatedCPU": "4",
        "EstimatedFreeDiskBytes": "20GB",
        "EstimatedMemory": "16GB",
        "container-image": RBE_DOCKER_TOOLS_IMAGE,
        "network": "external",
        "workload-isolation-type": "oci",
    },
    visibility = ["//debian:__pkg__"],
)

go_cov(
    name = "runsc_race_coverage",
    srcs = ["main.go"],
    out = "runsc_race_cov",
    gotags = [
        "kcov",
        "lockdep",
        "opensource",
    ],
    race = "on",
    static = True,
    visibility = ["//debian:__pkg__"],
    deps = [
        "//runsc/cli/maincli",
        "//runsc/version",
    ],
)
