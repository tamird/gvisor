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

# Both the plugin target and the build helper support Linux AMD64 only.
# The helper's configuration must describe the objects it reads. The config
# target uses the same Make inputs; qualification also checks the header bytes.
_MAKE_ARGS = [
    "'SHELL:=$(FOREIGN_CC_SHELL)'",
    "'CC:=$(CC)'",
    "'CPP:=$(CC) -E'",
    "'AR:=$(AR)'",
    "'LD:=$(LD)'",
    "'OBJDUMP:=$(if $(filter /%,$(OBJDUMP)),$(OBJDUMP),$(EXT_BUILD_ROOT)/$(OBJDUMP))'",
    "'TOOLCHAIN_CFLAGS:=$(CFLAGS)'",
    "'TOOLCHAIN_LDFLAGS:=$(LDFLAGS)'",
    "'HOST_EXTRA_CFLAGS:=$(CFLAGS)'",
    "'HOST_EXTRA_CPPFLAGS:=$(CPPFLAGS)'",
    "'EXTRA_HOST_LDFLAGS:=$(LDFLAGS)'",
    "'DPDK_TARGET:=$(RTE_TARGET)'",
    "DPDK_MACHINE=ivb",
    "EXTRA_CFLAGS='-g -O3 -fPIC -fno-omit-frame-pointer -DLOOK_ASIDE_BACKEND -Wno-error'",
]

_BUILD_DATA = [
    "@plugin_programs//:utilities",
    "@plugin_dpdk//:Makefile",
    "@plugin_dpdk//:sources",
]

_ENV = {
    "TMPDIR": "/tmp",
    "DPDK_MAKEFILE": "$(execpath @plugin_dpdk//:Makefile)",
    "OBJDUMP": "$(PLUGIN_OBJDUMP)",
    "RTE_TARGET": "x86_64-native-linuxapp-$(C_COMPILER)",
}

_COMMON = dict(
    build_shell = "@plugin_programs//:bin/bash",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    exec_properties = {
        "container-image": "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18",
        "recycle-runner": "false",
    },
    lib_source = ":sources",
    toolchains = ["@//external/tools/plugin-stack:toolchain"],
    visibility = ["//visibility:public"],
)

# Produce the real configuration without compiling DPDK or its build tools.
make(
    name = "pmd_config",
    args = _MAKE_ARGS + [
        "-C",
        "dpdk",
    ],
    build_data = _BUILD_DATA,
    env = _ENV,
    out_headers_only = True,
    postfix_script = """
mkdir -p $$INSTALLDIR/include
cp dpdk/_build/dpdk/$$RTE_TARGET/include/rte_config.h $$INSTALLDIR/include/
""",
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    targets = ["config"],
    **_COMMON
)

make(
    name = "libpluginstack",
    args = _MAKE_ARGS + [
        "'PMDINFO_GEN=$(PMDINFOGEN) $@ $@.pmd.c'",
        "DPDK_MAKE_TARGET=all",
        "'DPDK_LIBS_PATH=$(TLDK_ROOT)/dpdk/_build/dpdk/$(RTE_TARGET)/lib'",
    ],
    build_data = _BUILD_DATA + [
        ":pmd_config",
        "@plugin_dpdk//:pmdinfogen",
    ],
    env = _ENV | {
        "PMDINFOGEN": "$(execpath @plugin_dpdk//:pmdinfogen)",
        "PMD_CONFIG": "$(execpath :pmd_config)",
    },
    # The binary and configuration are additional qualification outputs.
    out_binaries = ["dpdk-pmdinfogen"],
    out_data_files = ["rte_config.h"],
    out_static_libs = ["libpluginstack.a"],
    postfix_script = """
cmp $$PMD_CONFIG/rte_config.h dpdk/_build/dpdk/$$RTE_TARGET/include/rte_config.h
mkdir -p $$INSTALLDIR/lib $$INSTALLDIR/include $$INSTALLDIR/bin
cp -L libtldk.a $$INSTALLDIR/lib/libpluginstack.a
cp -L $$PMDINFOGEN $$INSTALLDIR/bin/dpdk-pmdinfogen
cp $$PMD_CONFIG/rte_config.h $$INSTALLDIR/rte_config.h
""",
    target_compatible_with = select({
        ":plugin_tldk_condition": [
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
    targets = ["libtldk.a"],
    **_COMMON
)
