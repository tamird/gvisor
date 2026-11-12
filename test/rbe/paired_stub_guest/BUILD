load("//tools:defs.bzl", "cc_binary")

cc_binary(
    name = "pagesize_probe",
    testonly = True,
    srcs = ["probe.c"],
    linkopts = ["-static"],
    linkstatic = True,
    tags = ["manual"],
)
