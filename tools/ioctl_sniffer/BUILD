load("//tools:defs.bzl", "bzl_library", "cc_binary", "go_binary", "proto_library")
load(":defs.bzl", "gnu_filegroup")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

proto_library(
    name = "ioctl",
    srcs = ["ioctl.proto"],
    visibility = ["//tools/ioctl_sniffer:__subpackages__"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        "//tools:defs_bzl",
        "@with_cfg.bzl//:with_cfg",
    ],
)

cc_binary(
    name = "ioctl_hook",
    srcs = [
        "ioctl_hook.cc",
        "ioctl_hook.h",
        "sniffer_bridge.cc",
        "sniffer_bridge.h",
    ],
    # Needed to support being used when LD_PRELOAD'd into binaries that are
    # built with a libc library that doesn't support RELR relocations
    # (such as nvidia-smi).
    features = ["-enable_relr"],
    linkshared = True,
    deps = [
        ":ioctl_cc_proto",
        "@com_google_absl//absl/strings",
    ],
)

gnu_filegroup(
    name = "ioctl_hook_gnu",
    srcs = [":ioctl_hook"],
)

go_binary(
    name = "run_sniffer",
    srcs = [
        "ld.go",
        "run_sniffer.go",
    ],
    embedsrcs = [
        # Gazelle cannot infer the transitioned dependency from the embedded
        # filename, libioctl_hook.so.
        ":ioctl_hook_gnu",  # keep
    ],
    # Disable nogo tests because this has C++ dependencies.
    nogo = False,
    static = True,
    visibility = [
        "//:sandbox",
    ],
    deps = [
        "//pkg/log",
        "//tools/ioctl_sniffer/sniffer",
    ],
)
