load("@rules_cc//cc:cc_binary.bzl", "cc_binary")
load("@rules_cc//cc:cc_library.bzl", "cc_library")

exports_files(["Makefile"])

filegroup(
    name = "sources",
    srcs = glob(
        ["**"],
        exclude = ["BUILD.bazel"],
    ),
    visibility = ["//visibility:public"],
)

config_setting(
    name = "musl",
    constraint_values = ["@llvm//constraints/libc:musl"],
)

cc_library(
    name = "pmd_headers",
    hdrs = glob([
        "lib/librte_eal/common/include/*.h",
        "lib/librte_pci/*.h",
    ]),
    includes = [
        "lib/librte_eal/common/include",
        "lib/librte_pci",
    ],
    deps = [
        "@libpluginstack//:pmd_config",
        "@plugin_programs//:bsd_queue",
    ],
)

# foreign_cc's build_data places this ordinary binary in the execution config.
cc_binary(
    name = "pmdinfogen",
    srcs = [
        "buildtools/pmdinfogen/pmdinfogen.c",
        "buildtools/pmdinfogen/pmdinfogen.h",
    ],
    features = select({
        ":musl": ["fully_static_link"],
        "//conditions:default": [],
    }),
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    visibility = ["//visibility:public"],
    deps = [":pmd_headers"],
)
