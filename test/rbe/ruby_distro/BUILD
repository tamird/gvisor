load("@rules_python//python:defs.bzl", "py_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_test(
    name = "image_identity_test",
    size = "large",
    srcs = [
        "image_archive.py",
        "image_identity_test.py",
    ],
    args = [
        "$(locations //images/image-test/ruby:qualification_ruby_archives)",
        "$(locations //images/image-test/ruby:qualification_ruby_contexts)",
    ],
    data = glob(["inputs/*.json"]) + [
        "//images/image-test/ruby:qualification_ruby_archives",
        "//images/image-test/ruby:qualification_ruby_contexts",
    ],
    imports = ["."],
    tags = ["manual"],
)
