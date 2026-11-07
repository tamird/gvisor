load("@rules_python//python:py_test.bzl", "py_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_test(
    name = "buildkite_test",
    srcs = [
        "buildkite.py",
        "buildkite_test.py",
    ],
    imports = ["."],
)

py_test(
    name = "offline_observation_test",
    srcs = ["buildkite.py", "offline_observation_test.py"],
    imports = ["."],
)
