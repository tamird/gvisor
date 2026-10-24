load("//tools/nogo:defs.bzl", "nogo_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

nogo_test(
    name = "validate_nogo",
    config = "//:nogo_config",
    tags = ["manual"],
    deps = ["@protoc-gen-validate//validate:validate_go"],
)

nogo_test(
    name = "docker_runtime_nogo",
    config = "//:nogo_config",
    tags = ["manual"],
    deps = ["@com_github_docker_docker//api/types/swarm/runtime"],
)
