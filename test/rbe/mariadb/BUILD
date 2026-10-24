load("@rules_python//python:defs.bzl", "py_test")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

package(default_visibility = ["//test/compatibility:__pkg__"])

sh_binary(
    name = "consumer",
    testonly = True,
    srcs = ["consumer.sh"],
)

py_test(
    name = "image_identity_test",
    size = "large",
    srcs = [
        "image_archive.py",
        "image_identity_test.py",
    ],
    args = [
        "$(locations //images/compatibility:qualification_mariadb_archives)",
        "$(locations //images/compatibility:qualification_mariadb_contexts)",
    ],
    data = glob(["upstream/*.json"]) + [
        "//images/compatibility:qualification_mariadb_archives",
        "//images/compatibility:qualification_mariadb_contexts",
    ],
    tags = ["manual"],
)
