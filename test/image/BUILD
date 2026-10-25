load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/docker:config.bzl", "IMAGE_VARIANTS")
load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config", "docker_test")
load("//tools:defs.bzl", "go_library")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

docker_test(
    name = "image_test",
    size = "large",
    srcs = [
        "image_test.go",
    ],
    cohort = "image",
    data = [
        "latin10k.txt",
        "mysql.sql",
        # Needed to invalidate the bazel cache in case of any code changes.
        "//:release",
    ],
    embed = [":image"],
    runtime_variants = IMAGE_VARIANTS,
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_cenkalti_backoff//:go_default_library",
        "@in_gopkg_yaml_v3//:go_default_library",
    ],
)

go_library(
    name = "image",
    srcs = ["image.go"],
)

sh_binary(
    name = "qualification_ruby_consumer",
    testonly = True,
    srcs = ["ruby_consumer.sh"],
)

docker_daemon_config(
    name = "qualification_ruby_native_config",
    testonly = True,
    images = ["//test/docker:images_image_test_ruby_amd64_tar"],
    runtime = None,
    tags = ["manual"],
)

docker_command_test(
    name = "qualification_ruby_native_test",
    size = "large",
    command = ":qualification_ruby_consumer",
    command_args = [
        "$(rootpath //test/docker:images_image_test_ruby_amd64_tar)",
        "$(rootpath :image_test)",
        "--runtime=runc",
        "-test.run=^TestRuby$",
        "-test.v",
    ],
    data = [
        ":image_test",
        "//test/docker:images_image_test_ruby_amd64_tar",
    ],
    docker_config = ":qualification_ruby_native_config",
    exec_properties = docker_test_exec_properties(free_disk = "30GB", memory = "8GB"),
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)

docker_daemon_config(
    name = "qualification_ruby_systrap_config",
    testonly = True,
    images = ["//test/docker:images_image_test_ruby_amd64_tar"],
    runtime = "//:release",
    runtime_args = ["--platform=systrap"],
    tags = ["manual"],
)

docker_command_test(
    name = "qualification_ruby_systrap_test",
    size = "large",
    command = ":qualification_ruby_consumer",
    command_args = [
        "$(rootpath //test/docker:images_image_test_ruby_amd64_tar)",
        "$(rootpath :image_test)",
        "--runtime=runsc",
        "-test.run=^TestRuby$",
        "-test.v",
    ],
    data = [
        ":image_test",
        "//test/docker:images_image_test_ruby_amd64_tar",
    ],
    docker_config = ":qualification_ruby_systrap_config",
    exec_properties = docker_test_exec_properties(free_disk = "30GB", memory = "8GB"),
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)
