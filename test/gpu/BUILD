load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
load("//tools:defs.bzl", "go_test", "proto_library")
load("//tools/bazeldefs:platforms.bzl", "network_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# These frontends exercise an already installed driver through an owned daemon.
# They do not install the latest driver or qualify the public all-driver matrix.
[
    docker_daemon_config(
        name = "smoke_" + mode + "_config",
        testonly = True,
        images = [
            "//test/docker:cuda_samples_source_amd64",
            "//test/docker:cuda_samples_12_8_source_amd64",
        ],
        runtime = runtime,
        runtime_args = args,
        tags = ["manual"],
    )
    for mode, runtime, args in [
        ("runc", None, []),
        (
            "runsc",
            "//:release",
            ["--nvproxy=true"],
        ),
    ]
]

[
    docker_command_test(
        name = name,
        # Match the existing smoke_test and sr_test default action timeout.
        size = "medium",
        command = command,
        command_args = ["-test.v"],
        docker_config = config,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        tags = [
            "external",
            "manual",
            "no-remote-exec",
            "no-sandbox",
            "requires-gpu",
        ],
        target_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
    )
    for name, command, config in [
        ("smoke_runc_owned", ":smoke_test", ":smoke_runc_config"),
        ("smoke_runsc_owned", ":smoke_test", ":smoke_runsc_config"),
        ("sr_runsc_owned", ":sr_test", ":smoke_runsc_config"),
    ]
]

test_suite(
    name = "existing_driver_smoke",
    tags = ["manual"],
    tests = [
        ":smoke_runc_owned",
        ":smoke_runsc_owned",
        ":sr_runsc_owned",
    ],
    visibility = ["//:sandbox"],
)

go_test(
    name = "smoke_test",
    srcs = ["smoke_test.go"],
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = ["//pkg/test/dockerutil"],
)

go_test(
    name = "pytorch_test",
    srcs = ["pytorch_test.go"],
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = ["//pkg/test/dockerutil"],
)

go_test(
    name = "textgen_test",
    srcs = ["textgen_test.go"],
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    embedsrcs = ["gvisor.png"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/gpu/ollama",
    ],
)

go_test(
    name = "sglang_test",
    srcs = ["sglang_test.go"],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/gpu/sglang",
    ],
)

go_test(
    name = "sr_test",
    srcs = ["sr_test.go"],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/context",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
    ],
)

go_test(
    name = "cuda_test",
    timeout = "eternal",  # YES_I_REALLY_NEED_AN_ETERNAL_TEST
    srcs = ["cuda_test.go"],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/gpu/cuda",
    ],
)

go_test(
    name = "cuda_12_8_test",
    timeout = "eternal",  # YES_I_REALLY_NEED_AN_ETERNAL_TEST
    srcs = ["cuda_12_8_test.go"],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/gpu/cuda",
    ],
)

go_test(
    name = "ffmpeg_test",
    srcs = ["ffmpeg_test.go"],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = ["//pkg/test/dockerutil"],
)

go_test(
    name = "imagegen_test",
    srcs = ["imagegen_test.go"],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = ["//test/gpu/stablediffusion"],
)

go_test(
    name = "sniffer_test",
    srcs = ["sniffer_test.go"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = ["//pkg/test/dockerutil"],
)

go_test(
    name = "nccl_test",
    srcs = ["nccl_test.go"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = ["//pkg/test/dockerutil"],
)

go_test(
    name = "triton_test",
    srcs = ["triton_test.go"],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/gpu/triton",
    ],
)

proto_library(
    name = "gpu_driver_versions",
    testonly = True,
    srcs = ["gpu_driver_versions.proto"],
    visibility = [
        "//:sandbox",
    ],
)

go_test(
    name = "cos_gpu_compatibility_test",
    srcs = ["cos_gpu_compatibility_test.go"],
    # Prepared by tools/gpu/cos_metadata_input.sh. Compilation does not need a
    # catalog; executing this explicit manual test fails if none was supplied.
    # buildifier: disable=constant-glob
    data = glob(
        ["cos_metadata_input/images.json"],
        allow_empty = True,
    ),
    exec_properties = network_test_exec_properties(),
    tags = [
        "external",  # Driver metadata is fetched live, so results cannot be cached.
        "manual",
        "noguitar",
        "notap",
        "requires-network",
    ],
    deps = [
        ":gpu_driver_versions_go_proto",
        "//pkg/sentry/devices/nvproxy",
        "//pkg/test/testutil",
        "@org_golang_google_protobuf//encoding/prototext:go_default_library",
    ],
)
