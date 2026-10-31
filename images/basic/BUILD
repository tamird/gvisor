load("@rules_img//img:image.bzl", "image_index", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("@rules_rs//rs:rust_binary.bzl", "rust_binary")
load("//images:img.bzl", "image_outputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

image_manifest(
    name = "alpine",
    base = "@test_alpine",
    tags = ["manual"],
)

image_outputs(
    name = "alpine",
    image = ":alpine",
    image_name = "basic/alpine",
)

image_manifest(
    name = "busybox",
    base = "@test_busybox",
    tags = ["manual"],
)

image_outputs(
    name = "busybox",
    image = ":busybox",
    image_name = "basic/busybox",
)

# The integration test checks that mounting /tmp preserves image contents.
image_layer(
    name = "tmpfile_layer",
    srcs = {"/tmp/foo/file.txt": "tmpfile/file.txt"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    # Keep the base image's sticky /tmp when adding its child directory.
    file_metadata = {"/tmp": file_metadata(mode = "1777")},
)

image_manifest(
    name = "tmpfile",
    base = "@test_alpine",
    layers = [":tmpfile_layer"],
    tags = ["manual"],
)

image_outputs(
    name = "tmpfile",
    image = ":tmpfile",
    image_name = "basic/tmpfile",
)

# Build the example with Rust's self-contained musl runtime.
rust_binary(
    name = "rust_hello_world",
    srcs = ["rust/helloworld.rs"],
    crate_name = "hello_world",
    edition = "2021",
    # The C++ runtime would override Rust's bundled unwind implementation.
    features = ["-static_link_cpp_runtimes"],
    rustc_flags = ["-Clink-self-contained=yes"],
    target_compatible_with = ["@rules_rs//rs/platforms/constraints:musl"],
)

image_layer(
    name = "rust_binary_layer",
    srcs = {"/app/target/release/hello-world": ":rust_hello_world"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0755"),
    include_runfiles = False,
)

image_layer(
    name = "rust_notice_layer",
    srcs = {
        "/usr/share/licenses/rust/COPYRIGHT-library.html": "@default_rust_toolchains//:library_notice",
        "/usr/share/licenses/rust/NOTICE": "rust/NOTICE",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
)

image_manifest(
    name = "rust_manifest",
    cmd = ["./target/release/hello-world"],
    layers = [
        ":rust_binary_layer",
        ":rust_notice_layer",
    ],
    tags = ["manual"],
    working_dir = "/app",
)

image_index(
    name = "rust",
    manifests = [":rust_manifest"],
    platforms = select({
        "@platforms//cpu:aarch64": ["@rules_rs//rs/platforms:aarch64-unknown-linux-musl"],
        "@platforms//cpu:x86_64": ["@rules_rs//rs/platforms:x86_64-unknown-linux-musl"],
    }),
    tags = ["manual"],
)

image_index(
    name = "rust_index",
    manifests = [":rust_manifest"],
    platforms = [
        "@rules_rs//rs/platforms:aarch64-unknown-linux-musl",
        "@rules_rs//rs/platforms:x86_64-unknown-linux-musl",
    ],
    tags = ["manual"],
)

image_outputs(
    name = "rust",
    image = ":rust",
    image_name = "basic/rust",
)
