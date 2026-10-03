load("@rules_python//python:defs.bzl", "py_binary")
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

_LANGUAGES = [
    "go",
    "javascript",
    "python",
    "ruby",
]

[
    codeql_analysis(
        name = language,
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
    srcs = [":" + language for language in _LANGUAGES],
    tags = ["manual"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "diagnostics",
    srcs = [":" + language for language in _LANGUAGES],
    output_group = "codeql_diagnostics",
    tags = ["manual"],
    visibility = ["//visibility:public"],
)
