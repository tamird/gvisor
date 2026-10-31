load("@bazel_skylib//rules:run_binary.bzl", "run_binary")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")

exports_files([
    "LICENSE",
    "NOTICE",
])

# C output preserves LuaJIT's embedded module symbol while allowing the target
# compiler to select the object ISA. Both pinned 64-bit VMs use GC64 bytecode.
run_binary(
    name = "wrk_bytecode",
    srcs = [
        "src/wrk.lua",
        "@luajit//:src/jit/bcsave.lua",
    ],
    outs = ["wrk_bytecode.c"],
    args = [
        "-b",
        "-t",
        "c",
        "-n",
        "wrk",
        "$(execpath src/wrk.lua)",
        "$(execpath wrk_bytecode.c)",
    ],
    # -b loads jit.bcsave, the only external Lua module needed to emit C.
    env = {"LUA_PATH": "$(execpath @luajit//:src/jit/bcsave.lua)"},
    tool = "@luajit//:luajit_cli",
)

cc_library(
    name = "wrk",
    srcs = [
        "src/ae.c",
        "src/aprintf.c",
        "src/hdr_histogram.c",
        "src/http_parser.c",
        "src/net.c",
        "src/script.c",
        "src/ssl.c",
        "src/stats.c",
        "src/tinymt64.c",
        "src/units.c",
        "src/zmalloc.c",
    ],
    hdrs = glob(["src/*.h"]),
    copts = [
        "-std=c99",
        "-O2",
    ],
    includes = ["src"],
    linkopts = ["-pthread"],
    local_defines = [
        "_REENTRANT",
        "_POSIX_C_SOURCE=200809L",
        "_DEFAULT_SOURCE",
    ],
    target_compatible_with = ["@platforms//os:linux"],
    # ae.c includes the selected platform implementation directly.
    textual_hdrs = glob(["src/ae_*.c"]),
    visibility = ["//visibility:public"],
    deps = [
        "@luajit",
        "@openssl//:ssl",
        "@zlib",
    ],
)

cc_binary(
    name = "wrk2",
    srcs = [
        "src/wrk.c",
        ":wrk_bytecode",
    ],
    copts = [
        "-std=c99",
        "-O2",
    ],
    # LuaJIT locates the embedded wrk module through dlsym on this executable.
    # Drop unused implicit toolchain libraries, retaining the required runtime.
    linkopts = [
        "-Wl,-E",
        "-Wl,--as-needed",
    ],
    linkstatic = True,
    local_defines = [
        "_REENTRANT",
        "_POSIX_C_SOURCE=200809L",
        "_DEFAULT_SOURCE",
    ],
    target_compatible_with = ["@platforms//os:linux"],
    visibility = ["//visibility:public"],
    deps = [":wrk"],
)
