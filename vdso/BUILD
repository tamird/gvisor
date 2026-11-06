# Description:
#   This VDSO is a shared library that provides the same interfaces as the
#   normal system VDSO (time, gettimeofday, clock_gettimeofday) but which uses
#   timekeeping parameters managed by the sandbox kernel.

load("@bazel_skylib//rules:native_binary.bzl", "native_test")
load("@rules_python//python:py_test.bzl", "py_test")
load("//tools:arch.bzl", "arch_genrule", "select_arch")
load("//tools:defs.bzl", "cc_flags_supplier", "cc_toolchain", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

genrule(
    name = "vdso",
    srcs = [
        "barrier.h",
        "compiler.h",
        "cycle_clock.h",
        "seqlock.h",
        "syscalls.h",
        "vdso.cc",
        "vdso_amd64.lds",
        "vdso_arm64.lds",
        "vdso_time.h",
        "vdso_time.cc",
    ],
    outs = [
        "vdso.so",
    ],
    cmd = "$(CC) $(CC_FLAGS) " +
          "-I. " +
          "-O2 " +
          "-std=c++11 " +
          "-fPIC " +
          "-fno-sanitize=all " +
          # Some toolchains enable stack protector by default. Disable it, the
          # VDSO has no hooks to handle failures.
          "-fno-stack-protector " +
          "-shared " +
          "-nostdlib " +
          "-Wl,-soname=linux-vdso.so.1 " +
          "-Wl,--hash-style=sysv " +
          "-Wl,--no-undefined " +
          "-Wl,-Bsymbolic " +
          "-Wl,-z,max-page-size=4096 " +
          "-Wl,-z,common-page-size=4096 " +
          select_arch(
              amd64 = "-Wl,-T$(location vdso_amd64.lds) ",
              arm64 = "-Wl,-T$(location vdso_arm64.lds) ",
              no_match_error = "unsupported architecture",
          ) +
          "-o $(location vdso.so) " +
          "$(location vdso.cc) " +
          "$(location vdso_time.cc)",
    features = ["-pie"],
    toolchains = [
        cc_toolchain,
        ":no_pie_cc_flags",
    ],
    visibility = ["//:sandbox"],
)

cc_flags_supplier(
    name = "no_pie_cc_flags",
    features = ["-pie"],
)

go_binary(
    name = "check_vdso",
    srcs = ["check/main.go"],
)

native_test(
    name = "vdso_test",
    src = ":check_vdso",
    args = [
        "--check-data",
        "--vdso=$(rootpath :vdso)",
    ],
    data = [":vdso"],
)

genrule(
    name = "vdso_original",
    srcs = [
        "barrier.h",
        "compiler.h",
        "cycle_clock.h",
        "seqlock.h",
        "syscalls.h",
        "vdso.cc",
        "original_vdso_amd64.lds",
        "original_vdso_arm64.lds",
        "vdso_time.h",
        "vdso_time.cc",
    ],
    outs = [
        "vdso_original.so",
    ],
    cmd = "$(CC) $(CC_FLAGS) " +
          "-I. " +
          "-O2 " +
          "-std=c++11 " +
          "-fPIC " +
          "-fno-sanitize=all " +
          # Some toolchains enable stack protector by default. Disable it, the
          # VDSO has no hooks to handle failures.
          "-fno-stack-protector " +
          "-shared " +
          "-nostdlib " +
          "-Wl,-soname=linux-vdso.so.1 " +
          "-Wl,--hash-style=sysv " +
          "-Wl,--no-undefined " +
          "-Wl,-Bsymbolic " +
          "-Wl,-z,max-page-size=4096 " +
          "-Wl,-z,common-page-size=4096 " +
          select_arch(
              amd64 = "-Wl,-T$(location original_vdso_amd64.lds) ",
              arm64 = "-Wl,-T$(location original_vdso_arm64.lds) ",
              no_match_error = "unsupported architecture",
          ) +
          "-o $(location vdso_original.so) " +
          "$(location vdso.cc) " +
          "$(location vdso_time.cc)",
    features = ["-pie"],
    toolchains = [
        cc_toolchain,
        ":no_pie_cc_flags",
    ],
    visibility = ["//:sandbox"],
)


genrule(
    name = "vdso_data",
    srcs = [
        "barrier.h",
        "compiler.h",
        "cycle_clock.h",
        "seqlock.h",
        "syscalls.h",
        "vdso.cc",
        "data_vdso_amd64.lds",
        "data_vdso_arm64.lds",
        "vdso_time.h",
        "vdso_time.cc",
    ],
    outs = [
        "vdso_data.so",
    ],
    cmd = "$(CC) $(CC_FLAGS) " +
          "-I. " +
          "-O2 " +
          "-std=c++11 " +
          "-fPIC " +
          "-fno-sanitize=all " +
          # Some toolchains enable stack protector by default. Disable it, the
          # VDSO has no hooks to handle failures.
          "-fno-stack-protector " +
          "-shared " +
          "-nostdlib " +
          "-Wl,-soname=linux-vdso.so.1 " +
          "-Wl,--hash-style=sysv " +
          "-Wl,--no-undefined " +
          "-Wl,-Bsymbolic " +
          "-Wl,-z,max-page-size=4096 " +
          "-Wl,-z,common-page-size=4096 " +
          select_arch(
              amd64 = "-Wl,-T$(location data_vdso_amd64.lds) ",
              arm64 = "-Wl,-T$(location data_vdso_arm64.lds) ",
              no_match_error = "unsupported architecture",
          ) +
          "-o $(location vdso_data.so) " +
          "$(location vdso.cc) " +
          "$(location vdso_time.cc)",
    features = ["-pie"],
    toolchains = [
        cc_toolchain,
        ":no_pie_cc_flags",
    ],
    visibility = ["//:sandbox"],
)


arch_genrule(
    name = "qualify_good",
    src = ":vdso",
    template = "qualify.good.%s.so",
)

arch_genrule(
    name = "qualify_baseline",
    src = ":vdso_original",
    template = "qualify.baseline.%s.so",
)

arch_genrule(
    name = "qualify_data",
    src = ":vdso_data",
    template = "qualify.data.%s.so",
)

py_test(
    name = "checker_controls",
    srcs = ["checker_controls.py"],
    args = ["$(rootpath :check_vdso)"],
    data = [
        ":check_vdso",
        ":qualify_baseline",
        ":qualify_data",
        ":qualify_good",
        "check/main.go",
        "legacy_checker.py",
    ],
    python_version = "PY3",
)
