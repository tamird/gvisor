load("@aspect_rules_lint//format:defs.bzl", "format_test")

package(default_applicable_licenses = ["//:license"])

format_test(
    name = "declared_formatting_preflight",
    srcs = [
        "//:MODULE.bazel",
        "//pkg/tcpip/seqnum:format_srcs",
        "//tools/buildkite:format_srcs",
    ],
    disable_git_attribute_checks = True,
    go = "@org_golang_x_tools//cmd/goimports",
    go_check_args = ["-format-only", "-d"],
    starlark = "@buildifier_prebuilt//:buildifier",
    tags = ["manual"],
)
