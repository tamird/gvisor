load("@bazel_skylib//rules:copy_file.bzl", "copy_file")
load("@rules_cc//cc:cc_library.bzl", "cc_library")
load("@rules_foreign_cc//foreign_cc:defs.bzl", "make")

exports_files(glob(["dpdk/dpdk-v18.11_patches/*.patch"]))

filegroup(
    name = "sources",
    srcs = [
        "Makefile",
        "dpdk/Makefile",
    ] + glob([
        "lib/**",
        "mk/**",
    ]),
)

copy_file(
    name = "queue_header",
    src = "@plugin_bsd_queue//file",
    out = "include/sys/queue.h",
)

cc_library(
    name = "bsd_queue",
    hdrs = [":queue_header"],
    includes = ["include"],
)

# The static Go binary transitions this dependency to the same musl toolchain.
# The existing plugin supports Linux AMD64; DPDK also runs helper executables
# during its build, so its execution platform must match that architecture.
make(
    name = "libpluginstack",
    # DPDK takes tools and flags as command-line variables. Capture the
    # foreign_cc environment before its recursive makefiles redefine them.
    args = [
        "'CC:=$(CC)'",
        "'CPP:=$(CC) -E'",
        "'AR:=$(AR)'",
        "'LD:=$(LD)'",
        "'OBJDUMP:=$(OBJDUMP)'",
        "'TOOLCHAIN_CFLAGS:=$(CFLAGS)'",
        "'TOOLCHAIN_LDFLAGS:=$(LDFLAGS)'",
        "'HOST_EXTRA_CFLAGS:=$(CFLAGS)'",
        "'HOST_EXTRA_CPPFLAGS:=$(CPPFLAGS)'",
        "'EXTRA_HOST_LDFLAGS:=$(LDFLAGS)'",
        "RTE_TARGET=x86_64-native-linuxapp-clang",
        "DPDK_MACHINE=ivb",
        "EXTRA_CFLAGS='-g -O3 -fPIC -fno-omit-frame-pointer -DLOOK_ASIDE_BACKEND -Wno-error'",
    ],
    build_data = [
        "@llvm//tools:ld.lld",
        "@llvm//tools:llvm-objdump",
        "@plugin_dpdk//:Makefile",
        "@plugin_dpdk//:sources",
    ],
    env = {
        "DPDK_LD": "$(execpath @llvm//tools:ld.lld)",
        "DPDK_MAKEFILE": "$(execpath @plugin_dpdk//:Makefile)",
        "OBJDUMP": "$(execpath @llvm//tools:llvm-objdump)",
    },
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    # Build-time helpers must also run on workers without a musl interpreter.
    features = ["fully_static_link"],
    lib_source = ":sources",
    out_static_libs = ["libpluginstack.a"],
    resource_size = "small",
    target_compatible_with = select({
        "@//external/tools/plugin-stack:plugin_tldk": [
            "@llvm//constraints/libc:musl",
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
    targets = ["install-plugin"],
    visibility = ["//visibility:public"],
    deps = [":bsd_queue"],
)
