load("@rules_python//python:defs.bzl", "py_binary")
load("//tools:defs.bzl", "go_path")
load(":defs.bzl", "codeql_analysis")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_binary(
    name = "analyze",
    srcs = ["analyze.py"],
    deps = ["//tools/go_export:compiler"],
)

# Private analysis roots supplement the public Go export with real helper
# packages, protobufs and commands. Reuse its dependency graph rather than
# exporting another transitive copy of the runtime libraries.
go_path(
    name = "go_packages",
    testonly = True,
    include_data = False,
    include_transitive = False,
    mode = "archive",
    deps = [
        "//pkg/seccomp/precompiledseccomp/example/usage",
        "//pkg/seccomp/precompiledseccomp/example/usage:example_precompiled_gen_bin",
        "//pkg/segment/test:segment",
        "//pkg/tcpip/transport/tcp/test/e2e",
        "//runsc/boot/filter:filter_precompiled_gen_bin",
        "//runsc/boot/filter:filter_precompiled_gen_stubbed_bin",
        "//runsc/fsgofer/filter:filter_precompiled_gen_bin",
        "//runsc/fsgofer/filter:filter_precompiled_gen_stubbed_bin",
        "//test/gpu:gpu_driver_versions_go_proto",
        "//test/kubernetes:test_range_config_go_proto",
        "//test/packetimpact/dut/runsc",
        "//test/packetimpact/dut/runsc:devid",
        "//test/packetimpact/proto:posix_server_go_proto",
        "//test/runtimes/proctor",
        "//test/runtimes/proctor:proctor_bin",
        "//test/secbench",
        "//test/secbench:runner",
        "//tools/go_marshal/test",
        "//tools/go_marshal/test/escape",
        "//tools/gvisor2pcap",
        "//tools/ioctl_sniffer:ioctl_go_proto",
        "//tools/nvidia_driver_differ:run_differ",
        "//tools/parsers",
        "//tools/parsers:parser",
    ],
)

genrule(
    name = "go_package_sources",
    testonly = True,
    srcs = [
        ":go_packages",
        "//:go.mod",
        "//:go.sum",
    ],
    outs = ["go_package_sources.zip"],
    cmd = "$(execpath //tools/go_export:assemble) --gopath $(execpath :go_packages) --output $@ --go-mod $(execpath //:go.mod) " +
          "$(execpath //:go.mod) $(execpath //:go.sum)",
    tools = ["//tools/go_export:assemble"],
)

_LANGUAGES = [
    "go",
    "javascript",
    "python",
    "ruby",
]

[
    codeql_analysis(
        name = language,
        testonly = language == "go",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = {
            "EstimatedCPU": "4",
            "EstimatedMemory": "16GB",
        },
        language = language,
        tags = ["manual"],
    )
    for language in _LANGUAGES
]

filegroup(
    name = "all",
    testonly = True,
    srcs = [":" + language for language in _LANGUAGES],
    tags = ["manual"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "diagnostics",
    testonly = True,
    srcs = [":" + language for language in _LANGUAGES],
    output_group = "codeql_diagnostics",
    tags = ["manual"],
    visibility = ["//visibility:public"],
)
