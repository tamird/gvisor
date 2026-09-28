load("//test/docker:defs.bzl", "docker_daemon_config", "docker_image_archive")
load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "hello",
    testonly = True,
    srcs = ["hello.go"],
    deps = [
        "//test/kubernetes/k8sctx",
        "//test/kubernetes/testcluster",
        "@io_k8s_api//core/v1:go_default_library",
    ],
)

go_test(
    name = "hello_test",
    srcs = ["hello_test.go"],
    library = ":hello",
    local = True,
    nogo = False,
    tags = [
        "noguitar",
        "notap",
    ],
    deps = ["//test/kubernetes/k8sctx/kubectlctx"],
)

docker_image_archive(
    name = "kind_node",
    architecture = "amd64",
    image = "kubernetes/node",
)

docker_daemon_config(
    name = "kind_docker_config",
    testonly = True,
    images = [":kind_node_tar"],
    tags = ["manual"],
)

go_test(
    name = "kind_test",
    size = "large",
    srcs = ["kind_test.go"],
    args = [
        "--docker_test_config=$(rootpath :kind_docker_config)",
        "--runtime=runc",
        "--kind=$(rootpath @kind_linux_amd64//file)",
        "--alpine-archive=$(rootpath //test/docker:images_basic_alpine_amd64_tar)",
    ],
    data = [
        ":kind_docker_config",
        "//test/docker:images_basic_alpine_amd64_tar",
        "@kind_linux_amd64//file",
    ],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    library = ":hello",
    rundir = ".",
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/kubernetes/k8sctx",
        "//test/kubernetes/testcluster",
        "@io_k8s_api//node/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:go_default_library",
        "@io_k8s_client_go//kubernetes:go_default_library",
        "@io_k8s_client_go//tools/clientcmd:go_default_library",
    ],
)

go_library(
    name = "driver",
    testonly = True,
    srcs = ["driver_version.go"],
    deps = [
        "//test/gpu:gpu_driver_versions_go_proto",
        "//test/kubernetes/k8sctx",
        "//test/kubernetes/testcluster",
        "@io_k8s_api//core/v1:go_default_library",
        "@org_golang_google_protobuf//encoding/prototext:go_default_library",
    ],
)

go_test(
    name = "driver_test",
    srcs = ["driver_version_test.go"],
    library = ":driver",
    local = True,
    tags = [
        "noguitar",
        "notap",
    ],
    deps = ["//test/kubernetes/k8sctx/kubectlctx"],
)

go_library(
    name = "cudatest",
    testonly = True,
    srcs = ["cuda_test_lib.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/kubernetes",
        "//test/kubernetes/k8sctx",
        "//test/kubernetes/testcluster",
        "@io_k8s_api//core/v1:go_default_library",
    ],
)

go_test(
    name = "cuda_test",
    srcs = ["cuda_test.go"],
    library = ":cudatest",
    local = True,
    tags = [
        "noguitar",
        "notap",
    ],
    deps = ["//test/kubernetes/k8sctx/kubectlctx"],
)
