"""Declared build actions for wrk2's pinned Linux LuaJIT source."""

load("@bazel_skylib//rules:run_binary.bzl", "run_binary")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")

# Preserve the library scan order from the pinned src/Makefile.
_LIBRARIES = [
    "src/lib_base.c",
    "src/lib_math.c",
    "src/lib_bit.c",
    "src/lib_string.c",
    "src/lib_table.c",
    "src/lib_io.c",
    "src/lib_os.c",
    "src/lib_package.c",
    "src/lib_debug.c",
    "src/lib_jit.c",
    "src/lib_ffi.c",
    "src/lib_buffer.c",
]

def luajit_targets(name):
    """Defines the pinned library and its execution-platform bytecode compiler.

    Args:
        name: Name of the public LuaJIT library.
    """
    native.exports_files(["COPYRIGHT"])
    headers = native.glob(["src/*.h"])
    cc_binary(
        name = "minilua",
        srcs = ["src/host/minilua.c"],
        linkopts = ["-lm"],
    )

    # buildvm executes on the build host but emits the target's VM. Its target
    # define must survive the tools transition, as HOST_CC does upstream.
    for arch, target, flags in [
        ("x64", "X64", "-D ENDIAN_LE -D P64 -D JIT -D FFI -D FPU -D HFABI -D VER=0"),
        ("arm64", "ARM64", "-D ENDIAN_LE -D P64 -D JIT -D FFI -D DUALNUM -D FPU -D HFABI -D VER=80"),
    ]:
        run_binary(
            name = "buildvm_arch_" + arch,
            srcs = native.glob(["dynasm/*.lua"]) + ["src/vm_" + arch + ".dasc"],
            outs = ["generated/" + arch + "/buildvm_arch.h"],
            args = ["$(execpath dynasm/dynasm.lua)"] + flags.split(" ") + [
                "-o",
                "$(execpath generated/{}/buildvm_arch.h)".format(arch),
                "$(execpath src/vm_{}.dasc)".format(arch),
            ],
            tool = ":minilua",
        )
        cc_binary(
            name = "buildvm_" + arch,
            srcs = [
                "src/host/buildvm.c",
                "src/host/buildvm_asm.c",
                "src/host/buildvm_fold.c",
                "src/host/buildvm_lib.c",
                "src/host/buildvm_peobj.c",
                ":buildvm_arch_" + arch,
            ] + headers + native.glob([
                "src/host/*.h",
                "dynasm/*.h",
            ]),
            includes = ["dynasm", "src", "generated/" + arch],
            local_defines = [
                "LUAJIT_TARGET=LUAJIT_ARCH_" + target,
                "LUAJIT_OS=LUAJIT_OS_LINUX",
            ],
        )

    # Select the explicitly target-specialized tool before its exec transition.
    buildvm = select({
        "@platforms//cpu:x86_64": ":buildvm_x64",
        "@platforms//cpu:aarch64": ":buildvm_arm64",
    })
    for mode in ["bcdef", "ffdef", "libdef", "recdef", "folddef"]:
        inputs = ["src/lj_opt_fold.c"] if mode == "folddef" else _LIBRARIES
        output = "src/lj_" + mode + ".h"
        run_binary(
            name = "lj_" + mode,
            srcs = inputs,
            outs = [output],
            args = ["-m", mode, "-o", "$(execpath {})".format(output)] + [
                "$(execpath {})".format(src)
                for src in inputs
            ],
            tool = buildvm,
        )
    run_binary(
        name = "lj_vm",
        outs = ["src/lj_vm.S"],
        args = ["-m", "elfasm", "-o", "$(execpath src/lj_vm.S)"],
        tool = buildvm,
    )
    cc_library(
        name = name,
        # These patterns are exactly LJCORE_O + LJLIB_O in the pinned source;
        # the CLI, host tools and optional amalgamation are separate inputs.
        srcs = native.glob(["src/lj_*.c", "src/lib_*.c"]) + [":lj_vm"],
        hdrs = headers + [":" + "lj_" + mode for mode in ["bcdef", "ffdef", "libdef", "recdef", "folddef"]],
        copts = [
            "-O2",
            "-fomit-frame-pointer",
            "-funwind-tables",
            "-fno-stack-protector",
            "-U_FORTIFY_SOURCE",
        ],
        includes = ["src"],
        linkopts = ["-lm", "-ldl"],
        # Match Linux GCC's upstream external-unwind configuration explicitly.
        local_defines = ["LUA_CORE", "LUAJIT_UNWIND_EXTERNAL", "_FILE_OFFSET_BITS=64", "_LARGEFILE_SOURCE"],
        target_compatible_with = ["@platforms//os:linux"],
        visibility = ["//visibility:public"],
    )
    cc_binary(
        name = "luajit_cli",
        srcs = ["src/luajit.c"],
        linkopts = ["-Wl,-E"],
        deps = [":" + name],
        visibility = ["//visibility:public"],
    )
    native.exports_files(["src/jit/bcsave.lua"])
