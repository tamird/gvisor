load("//tools:defs.bzl", "cc_library")

package(default_visibility = ["//visibility:public"])

genrule(
    name = "header",
    outs = ["generated/header.h"],
    cmd = "printf 'int generated();\\n' > $@",
)

genrule(
    name = "source",
    outs = ["generated/source.cc"],
    cmd = "printf 'int generated() { return 7; }\\n' > $@",
)

cc_library(
    name = "dep",
    srcs = [":source"],
    hdrs = [":header"],
    include_prefix = "probe",
    strip_include_prefix = "generated",
)
