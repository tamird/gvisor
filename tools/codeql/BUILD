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
    for language in [
        "go",
        "javascript",
        "python",
        "ruby",
    ]
]

filegroup(
    name = "all",
    srcs = [
        ":go",
        ":javascript",
        ":python",
        ":ruby",
    ],
    tags = ["manual"],
    visibility = ["//visibility:public"],
)
