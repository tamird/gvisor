load("@rules_foreign_cc//foreign_cc:defs.bzl", "make")
load("@//external/tools/plugin-stack:toolchain.bzl", "plugin_toolchain")

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

_DPDK_ENV = {
    "DPDK_MAKEFILE": "$(execpath @plugin_dpdk//:Makefile)",
    "RTE_TARGET": "x86_64-native-linuxapp-$(C_COMPILER)",
}

_STATIC_GNU = "@llvm//constraints/glibc_linkage:static"

plugin_toolchain(
    name = "toolchain",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
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
        "'OBJDUMP:=$(if $(filter /%,$(OBJDUMP)),$(OBJDUMP),$(EXT_BUILD_ROOT)/$(OBJDUMP))'",
        "'TOOLCHAIN_CFLAGS:=$(CFLAGS)'",
        "'TOOLCHAIN_LDFLAGS:=$(LDFLAGS)'",
        "'HOST_EXTRA_CFLAGS:=$(CFLAGS)'",
        "'HOST_EXTRA_CPPFLAGS:=$(CPPFLAGS)'",
        "'DPDK_TARGET:=$(RTE_TARGET)'",
        "DPDK_MACHINE=ivb",
        "EXTRA_CFLAGS='-g -O3 -fPIC -fno-omit-frame-pointer -DLOOK_ASIDE_BACKEND -Wno-error'",
    ] + select({
        # Static glibc uses the unwinder even for C stdio. The foreign Make
        # link receives the toolchain flags but not Bazel's runtime archives.
        _STATIC_GNU: ["'EXTRA_HOST_LDFLAGS:=$(LDFLAGS) $(if $(filter /%,$(DPDK_HOST_UNWIND)),$(DPDK_HOST_UNWIND),$(EXT_BUILD_ROOT)/$(DPDK_HOST_UNWIND))'"],
        "//conditions:default": ["'EXTRA_HOST_LDFLAGS:=$(LDFLAGS)'"],
    }),
    build_data = [
        "@plugin_dpdk//:Makefile",
        "@plugin_dpdk//:sources",
    ] + select({
        _STATIC_GNU: [
            "@llvm//runtimes/libunwind:libunwind.static",
            "@llvm//tools:llvm-objdump",
        ],
        "//conditions:default": [],
    }),
    env = select({
        _STATIC_GNU: _DPDK_ENV | {
            "DPDK_HOST_UNWIND": "$(execpath @llvm//runtimes/libunwind:libunwind.static)",
            "OBJDUMP": "$(execpath @llvm//tools:llvm-objdump)",
        },
        "//conditions:default": _DPDK_ENV | {"OBJDUMP": "$(PLUGIN_OBJDUMP)"},
    }),
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
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
    toolchains = [":toolchain"],
    visibility = ["//visibility:public"],
)
