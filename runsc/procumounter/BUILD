load("//test/rbe/tcg:defs.bzl", "arm64_tcg_test")
load("//tools:defs.bzl", "cc_flags_supplier", "cc_linker", "go_test", "namespace_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(
    ["procumounter.c"],
    visibility = ["//:__pkg__"],
)

# Tell build_cleaner not to create an unnecessary cc_library for
# procumounter.c.
filegroup(
    name = "ignore_srcs",
    srcs = ["procumounter.c"],
    tags = ["ignore_srcs"],
)

# gvisor-proc-umounter must remain a statically-linked, freestanding (no libc)
# C binary that fits in a single 4KiB page. Read its comments as to why.
genrule(
    name = "procumounter_binary",
    srcs = ["procumounter.c"],
    outs = ["gvisor-proc-umounter"],
    cmd = "$(CC) $(CC_FLAGS) " +
          "--ld-path=$(execpath {}) ".format(cc_linker) +
          "-Os " +
          "-ffreestanding " +
          "-fno-sanitize=all " +
          "-fno-stack-protector " +
          "-fno-asynchronous-unwind-tables " +
          "-fno-unwind-tables " +
          "-static " +
          "-nostdlib " +
          "-nostartfiles " +
          "-Wl,--build-id=none " +
          "-Wl,--gc-sections " +
          "-Wl,-s " +
          "-Wl,-n " +
          "-o $(execpath gvisor-proc-umounter) " +
          "$(execpath procumounter.c)",
    executable = True,
    features = ["-pie"],
    toolchains = [":no_pie_cc_flags"],
    tools = [cc_linker],
    visibility = ["//visibility:public"],
)

cc_flags_supplier(
    name = "no_pie_cc_flags",
    features = ["-pie"],
)

go_test(
    name = "procumounter_test",
    size = "small",
    srcs = ["procumounter_test.go"],
    data = [":procumounter_binary"],
    exec_properties = namespace_test_exec_properties(),
    deps = [
        "//pkg/test/testutil",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

# Run the complete owner with its ARM64 umounter in a guest that permits user
# and mount namespaces. Preserve the owner's size, timeout and shard count.
arm64_tcg_test(
    name = "procumounter_arm64_tcg_test",
    size = "small",
    guest_env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    image = "//test/rbe/tcg:rc_guest",
    payload = ":procumounter_test_arm64",
    run_under = "//test/rbe:worker_identity",
    tags = [],
    visibility = ["//test/rbe:__pkg__"],
)
