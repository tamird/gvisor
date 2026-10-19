load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/bazeldefs:go_runtime.bzl", "go_runtime_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "licensecheck",
    srcs = [
        "apt.go",
        "classify.go",
        "licensecheck.go",
        "modules.go",
        "wheels.go",
    ],
    importpath = "gvisor.dev/gvisor/tools/licensecheck",
    marshal = False,
    stateify = False,
    visibility = ["//tools/licensecheck:__subpackages__"],
    deps = [
        "@com_github_google_licensecheck//:go_default_library",
        "@in_gopkg_yaml_v3//:go_default_library",
        "@org_golang_x_mod//modfile:go_default_library",
        "@org_golang_x_mod//module:go_default_library",
        "@org_golang_x_mod//semver:go_default_library",
    ],
)

go_test(
    name = "licensecheck_test",
    size = "small",
    srcs = [
        "apt_test.go",
        "licensecheck_test.go",
        "modules_test.go",
        "wheels_test.go",
    ],
    data = [
        "dependencies.yaml",
        "//governance:licensing.yaml",
    ],
    embed = [":licensecheck"],
    embedsrcs = ["testdata/apt-lock.json"] + glob(["testdata/*.txt"]),  # keep
    shared_compilation = True,
    visibility = ["//tools/codeql:__pkg__"],
)

# Exercise location transport without adding a test program or changing its
# assertions. The output directory affects only optional profiles, not tests.
go_runtime_test(
    name = "shared_runtime_contract_test",
    size = "small",
    src = ":licensecheck_test_compile",
    args = ["-test.outputdir=$(rootpath :dependencies.yaml)/.."],
    data = [
        "dependencies.yaml",
        "//governance:licensing.yaml",
    ],
    env = {"GVISOR_SHARED_RUNTIME_DATA": "$(rootpath :dependencies.yaml)"},
    tags = ["manual"],
)
