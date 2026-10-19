load("@rules_python//python:defs.bzl", "py_binary")
load(":defs.bzl", "codeql_analysis")
load(":go_roots.bzl", "GO_ANALYSIS_ROOTS")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["loaded-package-info.patch"])

py_binary(
    name = "analyze",
    srcs = [
        "analyze.py",
        "go_analysis.py",
    ],
    main = "analyze.py",
)

py_binary(
    name = "update_go_roots",
    srcs = ["update_go_roots.py"],
)

# Bounded provider-forwarding proof; the complete analyzer uses the generated
# manifest below. These are existing production declarations, including a
# testonly helper, generated proto, and configured static binary.
codeql_analysis(
    name = "go_inputs_check",
    testonly = True,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = {
        "EstimatedCPU": "4",
        "EstimatedMemory": "16GB",
    },
    language = "go",
    roots = {
        "//images:cuda_run_sample_codeql": "//images:cuda_run_sample",
        "//pkg/test/dockerutil:dockerutil_codeql": "//pkg/test/dockerutil:dockerutil",
        "//test/gpu:gpu_driver_versions_go_proto_codeql": "//test/gpu:gpu_driver_versions_go_proto",
        "//test/runtimes/proctor:proctor_bin_codeql": "//test/runtimes/proctor:proctor_bin",
        "//tools/licensecheck:licensecheck_codeql": "//tools/licensecheck:licensecheck",
    },
    tags = ["manual"],
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
        roots = GO_ANALYSIS_ROOTS if language == "go" else None,
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
