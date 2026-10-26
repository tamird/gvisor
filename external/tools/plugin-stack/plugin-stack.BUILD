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

config_setting(
    name = "plugin_tldk_condition",
    values = {"define": "plugin_tldk=true"},
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
        "'BAZEL_LD:=$(LD)'",
        "'OBJDUMP:=$(if $(filter /%,$(OBJDUMP)),$(OBJDUMP),$(EXT_BUILD_ROOT)/$(OBJDUMP))'",
        "'TOOLCHAIN_CFLAGS:=$(CFLAGS)'",
        "'TOOLCHAIN_LDFLAGS:=$(LDFLAGS)'",
        "'HOST_EXTRA_CFLAGS:=$(CFLAGS)'",
        "'HOST_EXTRA_CPPFLAGS:=$(CPPFLAGS)'",
        "'EXTRA_HOST_LDFLAGS:=$(LDFLAGS)'",
        "DPDK_MACHINE=ivb",
        "EXTRA_CFLAGS='-g -O3 -fPIC -fno-omit-frame-pointer -DLOOK_ASIDE_BACKEND -Wno-error'",
    ],
    build_data = [
        "@plugin_dpdk//:Makefile",
        "@plugin_dpdk//:sources",
    ],
    env = {
        "DPDK_MAKEFILE": "$(execpath @plugin_dpdk//:Makefile)",
        "OBJDUMP": "$(PLUGIN_OBJDUMP)",
        "RTE_TARGET": "x86_64-native-linuxapp-$(C_COMPILER)",
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
        ":plugin_tldk_condition": [
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
    targets = ["install-plugin"],
    toolchains = ["@//external/tools/plugin-stack:toolchain"],
    visibility = ["//visibility:public"],
    deps = [
        ":bsd_queue",
        "@libbacktrace//:backtrace",
    ],
)
