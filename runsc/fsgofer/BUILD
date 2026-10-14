load("//tools:defs.bzl", "go_library", "go_test", "namespace_test_exec_properties", "native_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "fsgofer",
    srcs = [
        "lisafs.go",
    ],
    visibility = ["//runsc:__subpackages__"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/cleanup",
        "//pkg/fd",
        "//pkg/fsutil",
        "//pkg/lisafs",
        "//pkg/log",
        "//pkg/marshal/primitive",
        "//runsc/config",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "lisafs_test",
    size = "small",
    srcs = ["lisafs_test.go"],
    deps = [
        ":fsgofer",
        "//pkg/context",
        "//pkg/lisafs",
        "//pkg/lisafs/testsuite",
        "//pkg/log",
        "//runsc/config",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

# LinkAt with AT_EMPTY_PATH requires CAP_DAC_READ_SEARCH, not merely UID 0.
native_test(
    name = "lisafs_hardlink_privileged_test",
    size = "small",
    src = ":lisafs_test",
    args = ["-test.run=^TestFSGofer$/^HardLink$"],
    env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    exec_properties = namespace_test_exec_properties(),
    tags = ["manual"],
    visibility = ["//test/rbe:__pkg__"],
)

# A privileged reader would skip the unreadable-directory regression.
native_test(
    name = "lisafs_permissions_nonroot_test",
    size = "small",
    src = ":lisafs_test",
    args = ["-test.run=^TestXattrWithoutReadPermission$"],
    env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    exec_properties = namespace_test_exec_properties(user = "nobody"),
    tags = ["manual"],
    visibility = ["//test/rbe:__pkg__"],
)
