load("//tools:arch.bzl", "arch_genrule", "select_arch")
load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

arch_genrule(
    name = "vdso_arch",
    architecture = select_arch(amd64 = "amd64", arm64 = "arm64"),
    src = "//vdso",
    template = "vdso_%s.so",
)

go_library(
    name = "vdsodata",
    srcs = [
        "vdsodata.go",
        "vdsodata_amd64.go",
        "vdsodata_arm64.go",
    ],
    embedsrcs = [
        ":vdso_arch",  # keep
    ],
    marshal = False,
    stateify = False,
    visibility = ["//pkg/sentry:internal"],
)
