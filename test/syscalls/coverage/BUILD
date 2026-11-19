load("@bazel_skylib//rules:native_binary.bzl", "native_test")
load("//test/runner:defs.bzl", "syscall_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

native_test(
    name = "uname_loader_test",
    src = "//test/syscalls/linux:uname_test",
    args = ["--gtest_filter=UnameTest.Sanity"],
    tags = ["manual"],
)

syscall_test(
    allow_native = False,
    args = ["--coverage-report-expectation=required"],
    debug = False,
    env = {
        "GTEST_FILTER": "UnameTest.Sanity",
        "GTEST_REPEAT": "100",
    },
    runner = "//test/runner:runner_coverage",
    runtime_fileset = "//test/runner:coverage_release",
    save = False,
    tags = ["manual"],
    test = "//test/syscalls/linux:uname_test",
)

alias(
    name = "uname_no_report",
    actual = "//test/syscalls/linux:uname_test",
)

syscall_test(
    allow_native = False,
    args = [
        "--collect-coverage=false",
        "--coverage-report-expectation=absent",
    ],
    debug = False,
    env = {
        "GTEST_FILTER": "UnameTest.Sanity",
        "GTEST_REPEAT": "100",
    },
    runner = "//test/runner:runner_coverage",
    runtime_fileset = "//test/runner:coverage_release",
    save = False,
    tags = ["manual"],
    test = ":uname_no_report",
)

syscall_test(
    allow_native = False,
    args = [
        "--collect-coverage=false",
        "--coverage-report-expectation=absent",
    ],
    debug = False,
    env = {
        "GTEST_FILTER": "KcovTest.Kcov",
        "GTEST_REPEAT": "100",
    },
    runner = "//test/runner:runner_coverage",
    runtime_fileset = "//test/runner:coverage_release",
    save = False,
    tags = ["manual"],
    test = "//test/syscalls/linux:kcov_test",
)
