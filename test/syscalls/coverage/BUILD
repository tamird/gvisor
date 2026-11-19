load("//test/runner:defs.bzl", "syscall_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

_COVERAGE = {
    "allow_native": False,
    "debug": False,
    "runner": "//test/runner:runner_coverage",
    "runtime_fileset": "//test/runner:coverage_release",
    "save": False,
    "tags": ["manual"],
}

syscall_test(
    args = ["--coverage-report-expectation=required"],
    env = {
        "GTEST_FILTER": "UnameTest.Sanity",
        "GTEST_REPEAT": "100",
    },
    test = "//test/syscalls/linux:uname_test",
    **_COVERAGE
)

alias(
    name = "uname_no_report",
    actual = "//test/syscalls/linux:uname_test",
)

syscall_test(
    args = [
        "--collect-coverage=false",
        "--coverage-report-expectation=absent",
    ],
    env = {
        "GTEST_FILTER": "UnameTest.Sanity",
        "GTEST_REPEAT": "100",
    },
    test = ":uname_no_report",
    **_COVERAGE
)

syscall_test(
    args = [
        "--collect-coverage=false",
        "--coverage-report-expectation=absent",
    ],
    env = {
        "GTEST_FILTER": "KcovTest.Kcov",
        "GTEST_REPEAT": "100",
    },
    test = "//test/syscalls/linux:kcov_test",
    **_COVERAGE
)
