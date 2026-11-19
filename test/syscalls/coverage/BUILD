load("//test/runner:defs.bzl", "syscall_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

syscall_test(
    test = "//test/syscalls/linux:uname_test",
    allow_native = False,
    runner = "//test/runner:runner_coverage",
    runtime_fileset = "//test/runner:coverage_release",
    save = False,
    tags = ["manual"],
)
