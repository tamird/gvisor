load("@rules_python//python:py_test.bzl", "py_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_test(
    name = "commit_update_test",
    size = "small",
    srcs = ["commit_update_test.py"],
    data = ["publish.sh"],
    # The transaction uses the worker's Git and Bash, not a hermetic toolchain.
    local = True,
    tags = [
        "no-cache",
        "no-remote",
    ],
)
