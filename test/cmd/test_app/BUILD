load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# The container and prctl tests execute this exact path in their runfiles.
native_binary(
    name = "test_app",
    testonly = 1,
    src = ":test_app_bin",
    out = "test_app",
    visibility = [
        "//runsc/container:__pkg__",
        "//test/syscalls/linux:__pkg__",
    ],
)

go_binary(
    name = "test_app_bin",
    testonly = 1,
    srcs = [
        "chardev.go",
        "clocks.go",
        "exec_thread.go",
        "fds.go",
        "hostinet_sr.go",
        "main.go",
        "segfault_unsafe.go",
        "state_workload.go",
        "state_workload_fs.go",
        "vdso_clock_amd64.s",
        "vdso_clock_arm64.s",
        "vdso_clock_unsafe.go",
        "xattr.go",
        "zombies.go",
    ],
    static = True,
    deps = [
        "//pkg/gvisordetect",
        "//pkg/test/testutil",
        "//pkg/unet",
        "//runsc/flag",
        "@com_github_creack_pty//:go_default_library",
        "@com_github_google_subcommands//:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

test_suite(
    name = "test_app_nogo",
    tags = ["nogo"],
    tests = [":test_app_bin_nogo"],
)
