load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "ownership_test",
    srcs = ["native.sh"],
    args = ["$(rootpath //test/syscalls/linux:shm_test)"],
    data = ["//test/syscalls/linux:shm_test"],
    exec_properties = {"dockerUser": "root"},
    tags = ["manual"],
)
