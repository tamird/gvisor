load("@rules_python//python:defs.bzl", "py_test")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

filegroup(
    name = "inventory_sentry",
    srcs = select({
        "//tools:gotsan": ["//runsc/cmd/sentry:gvisor_sentry_race_coverage"],
        "//conditions:default": ["//runsc/cmd/sentry:gvisor_sentry_coverage"],
    }),
)


sh_test(
    name = "legacy_tools_preflight_test",
    size = "small",
    timeout = "short",
    srcs = ["legacy_tools_preflight_test.sh"],
    args = [
        "$(rootpath @coverage_legacy_go_bootstrap//file)",
        "$(rootpath @coverage_legacy_go_source//file)",
        "$(rootpath //:go.mod)",
    ],
    data = [
        "//:go.mod",
        "@coverage_legacy_go_bootstrap//file",
        "@coverage_legacy_go_source//file",
    ],
    exec_compatible_with = ["//tools/bazeldefs:rbe_ubuntu24_c"],
    tags = [
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)


py_test(
    name = "legacy_parity_test",
    size = "large",
    srcs = ["inventory_test.py"],
    args = [
        "$(rlocationpath :inventory_sentry)",
        "--legacy-sentry=$(rlocationpath //runsc/cmd/sentry:legacy_current_sentry_cov)",
        "--source-archive=$(rlocationpath //:gopath)",
    ],
    data = [
        ":inventory_sentry",
        "//:gopath",
        "//runsc/cmd/sentry:legacy_current_sentry_cov",
    ],
    main = "inventory_test.py",
    stamp = -1,
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = ["@rules_python//python/runfiles"],
)


