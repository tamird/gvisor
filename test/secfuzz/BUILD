load("//tools:defs.bzl", "expand_template", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "secfuzz",
    testonly = 1,
    srcs = [
        "secfuzz.go",
        "secfuzz_covermeup.go",
        "secfuzz_nocoverageguidance.go",
    ],
    importpath = "gvisor.dev/gvisor/test/secfuzz",
    visibility = [
        "//:sandbox",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/abi/sentry",
        "//pkg/atomicbitops",
        "//pkg/bpf",
        "//pkg/seccomp",
        "//pkg/sync",
    ],
)

expand_template(
    name = "secfuzz_covermeup",
    out = "secfuzz_covermeup.go",
    substitutions = {
        "\t// GENERATED_LINES_INSERTED_HERE_THIS_IS_A_LOAD_BEARING_COMMENT": "\n".join([
            """\tcase {count}:
\t\tif covered[{index}] {{
\t\t\tfuzzee.coverage[{index}].Store(true)
\t\t}}
\t\tfallthrough""".format(count = i + 1, index = i)
            for i in range(4095, 0, -1)
        ]),
    },
    template = "secfuzz_covermeup.tmpl.go",
)
