load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//tools:defs.bzl", "bzl_library")
load(":defs.bzl", "clang_format_filelist")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//:sandbox"],
)

filegroup(
    name = "sources",
    srcs = [
        "//examples/seccheck:clang_format_srcs",
        "//images:clang_format_srcs",
        "//images/basic:clang_format_srcs",
        "//images/benchmarks/syscallbench:clang_format_srcs",
        "//pkg/sentry/platform/systrap/sysmsg:clang_format_srcs",
        "//runsc/fdparking:ignore_srcs",
        "//runsc/prewarmer:ignore_srcs",
        "//runsc/procumounter:ignore_srcs",
        "//runsc/sandbox/bpf:clang_format_srcs",
        "//test/benchmarks/tcp:clang_format_srcs",
        "//test/packetimpact/dut:clang_format_srcs",
        "//test/perf/linux:clang_format_srcs",
        "//test/runner/setup_container:clang_format_srcs",
        "//test/syscalls/linux:clang_format_srcs",
        "//test/syscalls/linux/rseq:clang_format_srcs",
        "//test/trace/workload:clang_format_srcs",
        "//test/util:clang_format_srcs",
        "//tools/cstate_wake_probe:clang_format_srcs",
        "//tools/ioctl_sniffer:clang_format_srcs",
        "//tools/nvidia_driver_differ:clang_format_srcs",
        "//tools/xdp/cmd/bpf:clang_format_srcs",
        "//tools/zstd:clang_format_srcs",
        "//vdso:clang_format_srcs",
    ],
)

# LLVM dispatches its multicall binary by the basename in argv[0].
native_binary(
    name = "clang_format_tool",
    src = "@llvm//tools:clang-format",
    out = "bin/clang-format",
    data = ["//:clang_format_config"],
)

platform_transition_filegroup(
    name = "host_clang_format_tool",
    srcs = [":clang_format_tool"],
    target_platform = "@platforms//host",
)

sh_binary(
    name = "format_clang",
    srcs = ["format_clang.sh"],
    args = [
        "$(rootpath :host_clang_format_tool)",
        "$(rootpaths :sources)",
    ],
    data = [
        ":host_clang_format_tool",
        ":sources",
        "//:clang_format_config",
    ],
)

clang_format_filelist(
    name = "source_files",
    srcs = [":sources"],
)

run_binary(
    name = "clang_format",
    srcs = [
        ":source_files",
        ":sources",
        "//:clang_format_config",
    ],
    args = [
        "--dry-run",
        "--Werror",
        "--files=$(execpath :source_files)",
    ],
    mnemonic = "ClangFormatCheck",
    progress_message = "Checking C and C++ formatting (fix with bazel run //tools/lint:format_clang)",
    stdout = "clang_format.report",
    tool = ":clang_format_tool",
)
