load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//test/kubernetes:__subpackages__"],
    licenses = ["notice"],
)

go_library(
    name = "httpbench",
    testonly = 1,
    srcs = ["httpbench.go"],
    importpath = "gvisor.dev/gvisor/test/kubernetes/benchmarks/httpbench",
    visibility = [
        "//test/kubernetes/benchmarks:__subpackages__",
    ],
    deps = [
        "//test/kubernetes",
        "//test/kubernetes/benchmetric",
        "//test/kubernetes/testcluster",
        "@io_k8s_api//core/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/api/resource:go_default_library",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:go_default_library",
    ],
)

# Exercise the real client arguments and parser without requiring a cluster.
go_test(
    name = "wrk2_test",
    size = "medium",
    srcs = ["wrk2_test.go"],
    args = ["--wrk2=$(rootpath @wrk2//:wrk2)"],
    data = ["@wrk2"],
    embed = [":httpbench"],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//pkg/test/testutil",
        "//test/kubernetes/testcluster",
    ],
)
