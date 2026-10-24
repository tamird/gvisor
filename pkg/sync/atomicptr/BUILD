load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/go_generics:defs.bzl", "go_template", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_template(
    name = "generic_atomicptr",
    srcs = ["generic_atomicptr.go"],
    types = [
        "Value",
    ],
    visibility = ["//:sandbox"],
)

go_template_instance(
    name = "atomicptr_int",
    out = "atomicptr_int.go",
    package = "atomicptr",
    suffix = "Int",
    template = ":generic_atomicptr",
    types = {
        "Value": "int",
    },
)

go_template(
    name = "generic_atomicptr_before",
    srcs = ["generic_atomicptr_before_unsafe.go"],
    types = ["Value"],
)

go_template_instance(
    name = "atomicptr_before_int",
    out = "atomicptr_before_int_unsafe.go",
    package = "atomicptr",
    suffix = "BeforeInt",
    template = ":generic_atomicptr_before",
    types = {
        "Value": "int",
    },
)

go_library(
    name = "atomicptr",
    srcs = [
        "atomicptr_before_int_unsafe.go",
        "atomicptr_int.go",
        "inline_probe.go",
    ],
    gc_goopts = [
        "-m=2",
        "-S",
    ],
)

go_test(
    name = "atomicptr_test",
    size = "small",
    srcs = [
        "atomicptr_test.go",
        "inline_probe_test.go",
    ],
    embed = [":atomicptr"],
    gc_goopts = [
        "-m=2",
        "-S",
    ],
)
