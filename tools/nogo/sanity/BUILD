load("//tools:defs.bzl", "go_library", "sh_test")
load("//tools/bazeldefs:defs.bzl", "sh_amd64_test", "sh_arm64_test")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_tags", "test_architecture_variants")
load("//tools/nogo:defs.bzl", "nogo_config", "nogo_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "sanity",
    srcs = ["sanity.go"],
    # This would fail, so we need to disable here. Instead, we manually
    # construct the nogo test below, and ensure that it is failing.
    nogo = False,
)

nogo_config(
    name = "sanity_config",
    srcs = ["sanity.yaml"],
)

nogo_test(
    name = "sanity_nogo",
    srcs = ["sanity.go"],
    config = ":sanity_config",
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    deps = [":sanity"],
)

# Only the explicit native variants require the embedded analyzer to match the
# target CPU. The ordinary test may use a different execution platform.
[
    nogo_test(
        name = "sanity_nogo_" + arch,
        srcs = ["sanity.go"],
        config = ":sanity_config",
        exec_compatible_with = [
            "@platforms//os:linux",
            cpu,
        ],
        tags = [
            "manual",
            "noguitar",
            "notap",
        ],
        visibility = ["//visibility:private"],
        deps = [":sanity"],
    )
    for arch, cpu in [
        ("amd64", "@platforms//cpu:x86_64"),
        ("arm64", "@platforms//cpu:aarch64"),
    ]
]

sh_test(
    name = "sanity_test",
    srcs = ["sanity_test.sh"],
    architectures = [],
    args = ["$(location :sanity_nogo)"],
    data = [":sanity_nogo"],
    tags = test_architecture_tags(
        [
            "amd64",
            "arm64",
        ],
        [],
    ),
)

[
    test_architecture_variants(
        name = "sanity_test",
        architectures = [arch],
        kwargs = {
            "srcs": ["sanity_test.sh"],
            "args": ["$(location :sanity_nogo_%s)" % arch],
            "data": [":sanity_nogo_%s" % arch],
        },
        test_rules = {arch: test_rule},
    )
    for arch, test_rule in [
        ("amd64", sh_amd64_test),
        ("arm64", sh_arm64_test),
    ]
]
