load("@bazel_skylib//rules:diff_test.bzl", "diff_test")
load("//governance/tools/maintainers:defs.bzl", "governance_files")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(
    ["licensing.yaml"],
    visibility = ["//tools/licensecheck:__pkg__"],
)

governance_files(name = "generated")

diff_test(
    name = "codeowners_test",
    failure_message = "Run make governance-regen to update the generated files.",
    file1 = "//:CODEOWNERS",
    file2 = ":generated/CODEOWNERS",
)

diff_test(
    name = "maintainers_test",
    failure_message = "Run make governance-regen to update the generated files.",
    file1 = "//:MAINTAINERS.md",
    file2 = ":generated/MAINTAINERS.md",
)

test_suite(
    name = "generated_files_test",
    tests = [
        ":codeowners_test",
        ":maintainers_test",
    ],
)
