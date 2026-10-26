load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
load("//tools:defs.bzl", "bzl_library", "go_test", "proto_library")
load("//tools/bazeldefs:platforms.bzl", "network_test_exec_properties")
load(":config.bzl", "GPU_ALL_IMAGES", "GPU_CUDA_IMAGES", "GPU_L4_IMAGES", "GPU_SMOKE_IMAGES")

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
        images = GPU_SMOKE_IMAGES,
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
            "exclusive",
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

bzl_library(
    name = "config_bzl",
    srcs = ["config.bzl"],
    visibility = ["//test/docker:__pkg__"],
)

# Keep the public Ubuntu all-capability and COS default-capability modes
# separate. The COS image prerequisite still includes ffmpeg, but its test is
# intentionally disabled in Make's cos-gpu-all-tests.
[
    docker_daemon_config(
        name = name + "_config",
        testonly = True,
        images = images,
        runtime = runtime,
        runtime_args = args,
        tags = ["manual"],
    )
    for name, images, runtime, args in [
        (
            "full_runsc",
            GPU_ALL_IMAGES,
            "//:release",
            [
                "--nvproxy=true",
                "--nvproxy-allowed-driver-capabilities=all",
            ],
        ),
        (
            "l4_runsc",
            GPU_L4_IMAGES,
            "//:release",
            [
                "--nvproxy=true",
                "--nvproxy-allowed-driver-capabilities=all",
            ],
        ),
        ("cos_runc", GPU_ALL_IMAGES, None, []),
        (
            "cos_runsc",
            GPU_ALL_IMAGES,
            "//:release",
            ["--nvproxy=true"],
        ),
        (
            "cuda_runsc",
            GPU_CUDA_IMAGES,
            "//:release",
            [
                "--nvproxy=true",
                "--nvproxy-allowed-driver-capabilities=all",
            ],
        ),
    ]
]

_FULL_COMMANDS = [
    ("pytorch", ":pytorch_test", []),
    ("textgen", ":textgen_test", []),
    ("imagegen", ":imagegen_test", []),
    ("sr", ":sr_test", []),
    ("nccl", ":nccl_test", []),
    ("ffmpeg", ":ffmpeg_test", []),
    (
        "vllm",
        "//test/gpu/vllm:vllm_test",
        [
            "-test.bench=.",
            "-test.benchtime=1x",
        ],
    ),
    ("sniffer", ":sniffer_test", []),
]

# These are real commands with their original assertion/benchmark bodies.
# Suites below group existing-driver executions; driver replacement belongs to
# the separate, serial host lifecycle command.
_COMMANDS = [
    (
        "full_" + name,
        command,
        ":full_runsc_config",
        args,
    )
    for name, command, args in _FULL_COMMANDS
] + [
    (
        "l4_" + name,
        command,
        ":l4_runsc_config",
        [],
    )
    for name, command in [
        ("sglang", ":sglang_test"),
        ("triton", ":triton_test"),
    ]
] + [
    (
        "cos_smoke_runc",
        ":smoke_test",
        ":cos_runc_config",
        ["--cos-gpu"],
    ),
    (
        "cos_smoke_runsc",
        ":smoke_test",
        ":cos_runsc_config",
        ["--cos-gpu"],
    ),
    (
        "cos_smoke_sr",
        ":sr_test",
        ":cos_runsc_config",
        ["--cos-gpu"],
    ),
] + [
    (
        "cos_" + name,
        command,
        ":cos_runsc_config",
        args + ["--cos-gpu"],
    )
    for name, command, args in _FULL_COMMANDS
    if name != "ffmpeg"
]

[
    docker_command_test(
        name = name + "_owned",
        size = "medium",
        command = command,
        command_args = ["-test.v"] + args,
        docker_config = config,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        tags = [
            "exclusive",
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
    for name, command, config, args in _COMMANDS
]

# Public partition flags select CUDA samples inside its one top-level Go test.
# Bazel sharding would instead remove that entire test from seven partitions.
[
    docker_command_test(
        name = "cuda_12_8_partition_%d_owned" % partition,
        timeout = "eternal",  # Match the existing CUDA target.
        command = ":cuda_12_8_test",
        command_args = [
            "-test.v",
            "--partition=%d" % partition,
            "--total_partitions=8",
        ],
        docker_config = ":cuda_runsc_config",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        shard_count = 1,
        tags = [
            "exclusive",
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
    for partition in range(1, 9)
]

[
    test_suite(
        name = "existing_driver_" + name,
        tags = ["manual"],
        tests = tests,
        visibility = ["//:sandbox"],
    )
    for name, tests in [
        (
            "all",
            [
                ":smoke_runc_owned",
                ":smoke_runsc_owned",
                ":sr_runsc_owned",
            ] + [":full_" + name + "_owned" for name, _, _ in _FULL_COMMANDS],
        ),
        (
            "l4",
            [
                ":l4_sglang_owned",
                ":l4_triton_owned",
            ],
        ),
        (
            "cos",
            [":" + name + "_owned" for name, _, _, _ in _COMMANDS if name.startswith("cos_")],
        ),
        (
            "cuda_12_8",
            [":cuda_12_8_partition_%d_owned" % partition for partition in range(1, 9)],
        ),
    ]
]

_SMOKE_FRONTENDS = [
    ":smoke_runc_owned",
    ":smoke_runsc_owned",
    ":sr_runsc_owned",
]

# Preserve ordered setup/smoke/full/checksum phases. These local host actions
# are never eligible for remote execution or a shared-host sandbox. The host
# command supplies the original public 55/90-minute coordinator timeout;
# nested frontend execution is direct, with no nested Bazel or Make.
[
    sh_test(
        name = "public_" + name,
        size = "enormous",
        srcs = ["cohort.sh"],
        args = [
            mode,
            "$(rootpath //tools/gpu:main)",
        ] + ["$(rootpath %s)" % command for command in commands],
        data = ["//tools/gpu:main"] + commands,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        tags = [
            "exclusive",
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
        visibility = ["//:sandbox"],
    )
    for name, mode, commands in [
        ("smoke", "smoke", _SMOKE_FRONTENDS),
        (
            "all",
            "all",
            _SMOKE_FRONTENDS + [":full_" + name + "_owned" for name, _, _ in _FULL_COMMANDS],
        ),
        (
            "l4",
            "l4",
            [
                ":l4_sglang_owned",
                ":l4_triton_owned",
            ],
        ),
        (
            "cos",
            "cos",
            [":" + name + "_owned" for name, _, _, _ in _COMMANDS if name.startswith("cos_")],
        ),
    ] + [
        (
            "cuda_12_8_partition_%d" % partition,
            "cuda",
            [":cuda_12_8_partition_%d_owned" % partition],
        )
        for partition in range(1, 9)
    ]
]

[
    sh_test(
        name = "public_all_drivers_partition_%d" % partition,
        size = "enormous",
        srcs = ["//tools/gpu:all_drivers_script"],
        args = [
            "--declared",
            "$(rootpath //tools/gpu:main)",
            str(partition),
            "8",
        ] + ["$(rootpath %s)" % command for command in _SMOKE_FRONTENDS],
        data = ["//tools/gpu:main"] + _SMOKE_FRONTENDS,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        tags = [
            "exclusive",
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
        visibility = ["//:sandbox"],
    )
    for partition in range(8)
]

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
