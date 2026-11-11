load("@bazel_lib//lib:copy_file.bzl", "copy_file")

package(default_applicable_licenses = ["//:license"])

# RunPodSandbox uses containerd's default image, even when the application
# image is already in the CRI image store.
_CONTAINERD_VERSIONS = {
    "3.8": ["1.7.31"],
    "3.10": ["2.0.8", "2.1.7"],
    "3.10.1": ["2.2.3"],
}

[
    filegroup(
        name = tag,
        srcs = ["@cri_pause_" + tag.replace(".", "_") + "//:load"],
        output_group = "tarball",
    )
    for tag in _CONTAINERD_VERSIONS
]

[
    copy_file(
        name = "containerd_" + version,
        src = ":" + tag,
        out = version + ".tar",
        allow_symlink = True,
    )
    for tag, versions in _CONTAINERD_VERSIONS.items()
    for version in versions
]

filegroup(
    name = "archives",
    testonly = True,
    srcs = [version + ".tar" for versions in _CONTAINERD_VERSIONS.values() for version in versions],
    visibility = ["//test/root:__pkg__"],
)
