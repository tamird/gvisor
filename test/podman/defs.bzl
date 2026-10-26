"""The public rootless Podman smoke test and its declared runtime tools."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//tools:defs.bzl", "namespace_test_exec_properties")

# Existing tools/images.mk source-hash tag 72904703eabf2248.
# Keep these paired with images/basic/podmantest/Dockerfile.
_IMAGES = {
    "amd64": "docker://us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/basic/podmantest_x86_64@sha256:fc038dd8b15492b53919aa59aa81cbe4ef0b8dd9ea3b5523ab58ca2597716405",
    "arm64": "docker://us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/basic/podmantest_aarch64@sha256:423781ce282367175283f69e85dbce58c03c075451eb5b2821b52cd795546edf",
}

def podman_test(name):
    """Declares the same smoke workload for each supported architecture.

    Args:
      name: Name of the suite containing the architecture-constrained tests.
    """
    for arch, image in _IMAGES.items():
        archive = "//test/docker:images_basic_alpine_" + arch + "_tar"
        sh_test(
            name = name + "_" + arch,
            size = "large",
            srcs = ["smoke.sh"],
            args = [
                "$(rootpath //:release)",
                "$(rootpath " + archive + ")",
                "gvisor.dev/images/basic/alpine:latest",
            ],
            data = ["//:release", archive],
            exec_properties = namespace_test_exec_properties(user = "nonroot", image = image),
            tags = ["manual"],
            target_compatible_with = [
                "@platforms//os:linux",
                "@platforms//cpu:" + ("x86_64" if arch == "amd64" else "aarch64"),
            ],
            visibility = ["//visibility:private"],
        )

    # Suite expansion runs the compatible test; command-line aliases only build
    # their referenced target and do not schedule its test action.
    native.test_suite(
        name = name,
        tags = ["manual"],
        tests = [":" + name + "_" + arch for arch in _IMAGES],
    )
