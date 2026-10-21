load("@rules_cc//cc:defs.bzl", "cc_library")

package(default_visibility = ["//visibility:public"])

exports_files(["COPYING"])

cc_library(
    name = "headers",
    hdrs = glob([
        "include/*.h",
        "include/*.hpp",
    ]),
    includes = ["include"],
)

# src/Makefile.am and compat/Makefile.am select these production sources.
# The distribution omits the Windows-only service implementation.
filegroup(
    name = "sources",
    srcs = glob(
        [
            "compat/*.c",
            "src/*.c",
            "src/*.cpp",
        ],
        exclude = [
            "src/checkdelay.c",
            "src/checkisoch.cpp",
            "src/checkpdfs.c",
            "src/igmp_querier.c",
        ],
    ),
)
