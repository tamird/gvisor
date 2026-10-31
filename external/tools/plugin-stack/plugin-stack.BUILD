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

config_setting(
    name = "musl",
    constraint_values = ["@llvm//constraints/libc:musl"],
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

_DPDK_ENV = {
    "DPDK_MAKEFILE": "$(execpath @plugin_dpdk//:Makefile)",
    "RTE_TARGET": "x86_64-native-linuxapp-$(C_COMPILER)",
}

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
    ] + select({
        ":musl": [
            # The compiler driver's executable link action is unsuitable for
            # DPDK's raw relocatable links.
            "'LD:=$(if $(filter /%,$(DPDK_LD)),$(DPDK_LD),$(EXT_BUILD_ROOT)/$(DPDK_LD))'",
            # The plugin always passes --no-pci; virtio-user still needs the PCI bus.
            "RTE_EAL_VFIO=n",
        ],
        "//conditions:default": ["'LD:=$(LD)'"],
    }) + [
        "'OBJDUMP:=$(if $(filter /%,$(OBJDUMP)),$(OBJDUMP),$(EXT_BUILD_ROOT)/$(OBJDUMP))'",
        "'TOOLCHAIN_CFLAGS:=$(CFLAGS)'",
        "'TOOLCHAIN_LDFLAGS:=$(LDFLAGS)'",
        "'HOST_EXTRA_CFLAGS:=$(CFLAGS)'",
        "'HOST_EXTRA_CPPFLAGS:=$(CPPFLAGS)'",
        "'EXTRA_HOST_LDFLAGS:=$(LDFLAGS)'",
        "'DPDK_TARGET:=$(RTE_TARGET)'",
        "DPDK_MACHINE=ivb",
        "EXTRA_CFLAGS='-g -O3 -fPIC -fno-omit-frame-pointer -DLOOK_ASIDE_BACKEND -Wno-error'",
    ],
    build_data = [
        "@plugin_dpdk//:Makefile",
        "@plugin_dpdk//:sources",
    ] + select({
        ":musl": [
            "@llvm//tools:ld.lld",
            "@llvm//tools:llvm-objdump",
        ],
        "//conditions:default": [],
    }),
    env = select({
        ":musl": _DPDK_ENV | {
            "DPDK_LD": "$(execpath @llvm//tools:ld.lld)",
            "OBJDUMP": "$(execpath @llvm//tools:llvm-objdump)",
        },
        "//conditions:default": _DPDK_ENV | {"OBJDUMP": "$(PLUGIN_OBJDUMP)"},
    }),
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    # DPDK executes build helpers on hosts without a musl interpreter.
    features = select({
        ":musl": ["fully_static_link"],
        "//conditions:default": [],
    }),
    lib_source = ":sources",
    out_static_libs = ["libpluginstack.a"],
    postfix_script = """
mkdir -p $$INSTALLDIR/lib $$INSTALLDIR/include
cp -L libtldk.a $$INSTALLDIR/lib/libpluginstack.a
""",
    target_compatible_with = select({
        ":plugin_tldk_condition": [
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
    targets = ["libtldk.a"],
    toolchains = ["@//external/tools/plugin-stack:toolchain"],
    visibility = ["//visibility:public"],
    deps = select({
        ":musl": [":bsd_queue"],
        "//conditions:default": [],
    }),
)
