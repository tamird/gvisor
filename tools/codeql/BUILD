load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load(":defs.bzl", "codeql_analysis")
load(":go_roots.bzl", "GO_ANALYSIS_EXCLUDED_MODES", "GO_ANALYSIS_EXCLUDED_ROOTS", "GO_ANALYSIS_ROOTS")

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

py_test(
    name = "go_analysis_test",
    srcs = [
        "go_analysis.py",
        "go_analysis_test.py",
        "update_go_roots.py",
    ],
    main = "go_analysis_test.py",
)

# Bounded provider-forwarding proof; the complete analyzer uses the generated
# manifest below. Include default/tag-excluded roots and source-only configured
# dispositions alongside the helper, generated proto and static binary proof.
codeql_analysis(
    name = "go_inputs_check",
    testonly = True,
    excluded_modes = {
        "//runsc:runsc-race": ["race"],
        "//runsc:runsc_coverage": ["coverage"],
        "//tools/checkescape/test3:test3-race": ["race"],
    },
    excluded_roots = {
        "//runsc:runsc-race_codeql": "//runsc:runsc-race",
        "//runsc:runsc_coverage_codeql": "//runsc:runsc_coverage",
        "//tools/checkescape/test3:test3-race_codeql": "//tools/checkescape/test3:test3-race",
    },
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
        "//runsc:runsc-plugin-stack_codeql": "//runsc:runsc-plugin-stack",
        "//runsc:runsc_codeql": "//runsc:runsc",
        "//test/gpu:gpu_driver_versions_go_proto_codeql": "//test/gpu:gpu_driver_versions_go_proto",
        "//test/runtimes/proctor:proctor_bin_codeql": "//test/runtimes/proctor:proctor_bin",
        "//tools/checkescape/test3:test3_codeql": "//tools/checkescape/test3:test3",
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
        excluded_modes = GO_ANALYSIS_EXCLUDED_MODES if language == "go" else None,
        excluded_roots = GO_ANALYSIS_EXCLUDED_ROOTS if language == "go" else None,
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
