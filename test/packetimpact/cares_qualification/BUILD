load("//tools:defs.bzl", "go_test")

go_test(
    name = "server_test",
    size = "medium",
    srcs = ["server_test.go"],
    data = ["//test/packetimpact/dut:posix_server"],
    deps = [
        "//pkg/test/testutil",
        "//test/packetimpact/proto:posix_server_go_proto",
        "@org_golang_google_grpc//:go_default_library",
        "@org_golang_google_grpc//credentials/insecure:go_default_library",
    ],
)
