load("//tools:defs.bzl", "go_library", "select_arch", "sh_test")
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

# sanity_test executes the generated script as data, so its embedded analyzer
# must match the test's architecture. Execution constraints cannot use select().
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

alias(
    name = "sanity_nogo",
    actual = select_arch(
        amd64 = ":sanity_nogo_amd64",
        arm64 = ":sanity_nogo_arm64",
    ),
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
)

sh_test(
    name = "sanity_test",
    srcs = ["sanity_test.sh"],
    args = ["$(location :sanity_nogo)"],
    data = [":sanity_nogo"],
)
