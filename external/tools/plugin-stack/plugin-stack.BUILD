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

# Both the plugin target and the build helper support Linux AMD64 only.
# pmdinfogen uses RTE_ARCH_64 for ELF layout and reads byte order from the ELF.
# Its build configuration need not enable the target's runtime drivers.
_MAKE_ARGS = [
    "'SHELL:=$(DPDK_BUILD_SHELL)'",
    "'CC:=$(CC)'",
    "'CPP:=$(CC) -E'",
    "'AR:=$(AR)'",
    "'LD:=$(if $(filter /%,$(PLUGIN_LD)),$(PLUGIN_LD),$(EXT_BUILD_ROOT)/$(PLUGIN_LD))'",
    "'OBJDUMP:=$(if $(filter /%,$(OBJDUMP)),$(OBJDUMP),$(EXT_BUILD_ROOT)/$(OBJDUMP))'",
    "'TOOLCHAIN_CFLAGS:=$(CFLAGS)'",
    "'TOOLCHAIN_LDFLAGS:=$(LDFLAGS)'",
    "'HOST_EXTRA_CFLAGS:=$(CFLAGS)'",
    "'HOST_EXTRA_CPPFLAGS:=$(CPPFLAGS)'",
    "'EXTRA_HOST_LDFLAGS:=$(LDFLAGS)'",
    "'DPDK_TARGET:=$(RTE_TARGET)'",
    "DPDK_MACHINE=ivb",
    # DPDK 18.11 uses pread64/pwrite64; musl requires this feature macro.
    "EXTRA_CPPFLAGS=-D_LARGEFILE64_SOURCE",
    "EXTRA_CFLAGS='-g -O3 -fPIC -fno-omit-frame-pointer -DLOOK_ASIDE_BACKEND -Wno-error'",
]

_BUILD_DATA = [
    "@plugin_programs//:utilities",
    "@plugin_dpdk//:Makefile",
    "@plugin_dpdk//:sources",
]

_ENV = {
    # Bash records its own absolute path. The declared applets are siblings.
    "DPDK_BUILD_SHELL": "$${BASH}",
    "PATH": "$${BASH%/*}",
    "TMPDIR": "/tmp",
    "DPDK_MAKEFILE": "$(execpath @plugin_dpdk//:Makefile)",
    "OBJDUMP": "$(PLUGIN_OBJDUMP)",
    "PLUGIN_LD": "$(PLUGIN_LD)",
    "RTE_TARGET": "x86_64-native-linuxapp-$(C_COMPILER)",
}

_EXEC_PROPERTIES = {
    "container-image": "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18",
    "recycle-runner": "false",
}

_EXEC_CONSTRAINTS = [
    "@platforms//cpu:x86_64",
    "@platforms//os:linux",
]

# Produce the real configuration without compiling DPDK or its build tools.
make(
    name = "pmd_config",
    args = _MAKE_ARGS + [
        "-C",
        "dpdk",
    ],
    build_data = _BUILD_DATA,
    build_shell = "@plugin_programs//:bin/bash",
    env = _ENV,
    exec_compatible_with = _EXEC_CONSTRAINTS,
    exec_properties = _EXEC_PROPERTIES,
    lib_source = ":sources",
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
    toolchains = ["@//external/tools/plugin-stack:toolchain"],
    visibility = ["//visibility:public"],
)

make(
    name = "libpluginstack",
    args = _MAKE_ARGS + [
        "DPDK_MAKE_TARGET=all",
        "'DPDK_LIBS_PATH=$(TLDK_ROOT)/dpdk/_build/dpdk/$(RTE_TARGET)/lib'",
    ] + select({
        ":musl": [],
        "//conditions:default": ["'PMDINFO_GEN=$(PMDINFOGEN) $@ $@.pmd.c'"],
    }),
    build_data = _BUILD_DATA + select({
        ":musl": [],
        "//conditions:default": [
            ":pmd_config",
            "@plugin_dpdk//:pmdinfogen",
        ],
    }),
    build_shell = "@plugin_programs//:bin/bash",
    env = select({
        ":musl": _ENV,
        "//conditions:default": _ENV | {
            "PMDINFOGEN": "$(execpath @plugin_dpdk//:pmdinfogen)",
            "PMD_CONFIG": "$(execpath :pmd_config)",
        },
    }),
    exec_compatible_with = _EXEC_CONSTRAINTS,
    exec_properties = _EXEC_PROPERTIES,
    # DPDK builds and executes pmdinfogen on Linux AMD64. A static musl
    # executable can run on the build worker without a dynamic loader.
    features = select({
        ":musl": ["fully_static_link"],
        "//conditions:default": [],
    }),
    lib_source = ":sources",
    # The binary and configuration are additional qualification outputs.
    out_binaries = ["dpdk-pmdinfogen"],
    out_data_files = ["rte_config.h"] + select({
        ":musl": [],
        "//conditions:default": ["pmd_rte_config.h"],
    }),
    out_static_libs = ["libpluginstack.a"],
    postfix_script = """
grep -qx "#define RTE_ARCH_64 1" dpdk/_build/dpdk/$$RTE_TARGET/include/rte_config.h
mkdir -p $$INSTALLDIR/lib $$INSTALLDIR/include $$INSTALLDIR/bin
cp -L libtldk.a $$INSTALLDIR/lib/libpluginstack.a
cp dpdk/_build/dpdk/$$RTE_TARGET/include/rte_config.h $$INSTALLDIR/rte_config.h
""" + select({
        ":musl": "cp -L dpdk/_build/dpdk/$$RTE_TARGET/app/dpdk-pmdinfogen $$INSTALLDIR/bin/dpdk-pmdinfogen",
        "//conditions:default": """
grep -qx "#define RTE_ARCH_64 1" $$PMD_CONFIG/rte_config.h
cp -L $$PMDINFOGEN $$INSTALLDIR/bin/dpdk-pmdinfogen
cp $$PMD_CONFIG/rte_config.h $$INSTALLDIR/pmd_rte_config.h
""",
    }),
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
        ":musl": ["@plugin_programs//:bsd_queue"],
        "//conditions:default": [],
    }),
)
