load("//tools:defs.bzl", "cc_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# These exercise the aspect explicitly, outside the normal lint selection.
cc_library(
    name = "direct_include",
    testonly = True,
    srcs = ["direct_include.cc"],
    tags = ["manual"],
)

cc_library(
    name = "indirect_include",
    testonly = True,
    srcs = ["indirect_include.cc"],
    implementation_deps = [":indirect_header"],
    tags = ["manual"],
)

cc_library(
    name = "indirect_header",
    testonly = True,
    hdrs = ["indirect.h"],
    tags = ["manual"],
)
