load("@rules_python//python:defs.bzl", "py_binary")
load("//tools:defs.bzl", "go_path")
load(":defs.bzl", "codeql_analysis")
load(":package_probe.bzl", "package_probe")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["loaded-package-info.patch"])

py_binary(
    name = "analyze",
    srcs = ["analyze.py"],
    deps = ["//tools/go_export:compiler"],
)

py_binary(
    name = "package_probe_runner",
    srcs = ["package_probe.py"],
    main = "package_probe.py",
)

_PACKAGE_PROBE_ROOTS = [
    "//pkg/tcpip/tests/integration:forward_test",
    "//pkg/tcpip/tests/integration:iptables_test",
    "//pkg/tcpip/header:header_test",
    "//pkg/tcpip/header:header_x_test",
    "//pkg/segment/test:segment_test",
    "//tools/go_generics/rules_tests:template_test",
    "//tools/licensecheck:licensecheck_test",
    "//test/runner:xml_test",
    "//external/tools/plugin-stack:backtrace_test",
]

# Investigate the existing package-loader contract before replacing full analysis.
package_probe(
    name = "package_probe",
    testonly = True,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = {
        "EstimatedCPU": "4",
        "EstimatedMemory": "16GB",
    },
    roots = _PACKAGE_PROBE_ROOTS,
    tags = ["manual"],
)

# Compare nine qualified owners and analyze Triton without its runtime archive.
package_probe(
    name = "compile_companion_probe",
    testonly = True,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = {
        "EstimatedCPU": "4",
        "EstimatedMemory": "16GB",
    },
    comparison_roots = _PACKAGE_PROBE_ROOTS,
    roots = [label + "_compile" for label in _PACKAGE_PROBE_ROOTS] + [
        "//test/rbe:triton_archive_integrity_test_compile",
    ],
    tags = ["manual"],
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
        "//images:cuda_12_8_run_sample",
        "//images:cuda_run_sample",
        "//pkg/ilist:ring",
        "//pkg/seccomp/precompiledseccomp/example/usage",
        "//pkg/seccomp/precompiledseccomp/example/usage:example_precompiled_gen_bin",
        "//pkg/segment/test:segment",
        "//pkg/sync/atomicptr",
        "//pkg/tcpip/transport/tcp/test/e2e",
        "//runsc/boot/filter:filter_precompiled_gen_bin",
        "//runsc/boot/filter:filter_precompiled_gen_stubbed_bin",
        "//runsc/boot/filter/dumpfilter",
        "//runsc/fsgofer/filter:filter_precompiled_gen_bin",
        "//runsc/fsgofer/filter:filter_precompiled_gen_stubbed_bin",
        "//runsc/prewarmer:fdtable_expander",
        "//runsc/specutils/safemount_test:safemount_runner",
        "//test/benchmarks/base",
        "//test/benchmarks/tcp:tcp_proxy",
        "//test/gpu:gpu_driver_versions_go_proto",
        "//test/kubernetes:test_range_config_go_proto",
        "//test/kubernetes/benchmarks:abslbuild",
        "//test/kubernetes/benchmarks:ffmpeg",
        "//test/kubernetes/benchmarks:grpc",
        "//test/kubernetes/benchmarks:gsutil",
        "//test/kubernetes/benchmarks:nginx",
        "//test/kubernetes/benchmarks:ollama",
        "//test/kubernetes/benchmarks:openclaw",
        "//test/kubernetes/benchmarks:postgresql",
        "//test/kubernetes/benchmarks:redis",
        "//test/kubernetes/benchmarks:rubydev",
        "//test/kubernetes/benchmarks:sglang",
        "//test/kubernetes/benchmarks:stablediffusion",
        "//test/kubernetes/benchmarks:startup",
        "//test/kubernetes/benchmarks:tensorflow",
        "//test/kubernetes/benchmarks:triton",
        "//test/kubernetes/benchmarks:vllm",
        "//test/kubernetes/benchmarks:wordpress",
        "//test/kubernetes/tests:cudatest",
        "//test/kubernetes/tests:driver",
        "//test/kubernetes/tests:hello",
        "//test/metricsviz/metricsviz_cli",
        "//test/moby:runner",
        "//test/packetimpact/dut",
        "//test/packetimpact/dut/runsc",
        "//test/packetimpact/dut/runsc:devid",
        "//test/packetimpact/proto:posix_server_go_proto",
        "//test/packetimpact/tests:socket_send_recv_helpers",
        "//test/root",
        "//test/runtimes/proctor",
        "//test/runtimes/proctor:proctor_bin",
        "//test/secbench",
        "//test/secbench:runner",
        "//tools/go_marshal/test",
        "//tools/go_marshal/test/escape",
        "//tools/go_marshal/test/external",
        "//tools/gvisor2pcap",
        "//tools/ioctl_sniffer:ioctl_go_proto",
        "//tools/ioctl_sniffer:run_sniffer",
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
