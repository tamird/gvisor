load("@rules_img//img:image.bzl", "image_index", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")
load("//tools:defs.bzl", "cc_binary", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "syscallbench",
    srcs = ["syscallbench.c"],
    copts = ["-O2"],
    linkopts = ["-static"],
    # The assembly loop and seccomp filters use the Linux x86-64 ABI.
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

image_layer(
    name = "syscallbench_layer",
    srcs = {"/usr/bin/syscallbench": ":syscallbench"},
    default_metadata = file_metadata(mode = "0755"),
    # The benchmark keeps the container alive while executing each mode.
    symlinks = {"/bin/sleep": "/bin/busybox"},
)

image_manifest(
    name = "syscallbench_manifest",
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":syscallbench_layer",
    ],
    tags = ["manual"],
    user = "0:0",
)

# The existing benchmark assembly supports only the Linux x86-64 target.
image_index(
    name = "syscallbench_image",
    manifests = [":syscallbench_manifest"],
    platforms = ["@llvm//platforms:linux_x86_64_musl"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)

image_outputs(
    name = "syscallbench",
    image = ":syscallbench_image",
    image_name = "benchmarks/syscallbench",
)

go_binary(
    name = "profilehelper",
    srcs = ["profile-helper/main.go"],
    pure = True,
    static = True,
)

image_layer(
    name = "profile_helper_layer",
    srcs = {"/usr/bin/profilehelper": ":profilehelper"},
    default_metadata = file_metadata(mode = "0755"),
)

image_manifest(
    name = "profile_helper_manifest",
    cmd = ["/usr/bin/profilehelper"],
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:helper_filesystem",
        "//images:musl_notice_layer",
        "//images:profile_coreutils_layer",
        ":profile_helper_layer",
    ],
    tags = ["manual"],
    user = "0:0",
)

image_index(
    name = "profile_helper_image",
    manifests = [":profile_helper_manifest"],
    # Select the static Linux target ABI independently of the execution host.
    platforms = select({
        "@platforms//cpu:aarch64": ["@llvm//platforms:linux_aarch64_musl"],
        "@platforms//cpu:x86_64": ["@llvm//platforms:linux_x86_64_musl"],
    }),
    tags = ["manual"],
)

image_outputs(
    name = "profile_helper",
    image = ":profile_helper_image",
    image_name = "benchmarks/profile-helper",
)

arch_genrule(
    name = "qualification_profile_helper_archives",
    src = ":profile_helper_archive",
    template = "qualification_profile_helper_archive_%s.tar",
)

arch_genrule(
    name = "qualification_profile_helper_contexts",
    src = ":profile_helper_context",
    template = "qualification_profile_helper_context_%s.tar",
)
